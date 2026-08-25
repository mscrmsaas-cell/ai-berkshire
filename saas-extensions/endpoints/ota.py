"""OTA 升级管理 API — 固件版本查询、升级触发、状态追踪。

FastAPI Router，对应 PostgreSQL 表：
  - ota_upgrade_records
  - ai_model_versions
  - glasses_devices (target_version 字段)

注册方式::

    from app.api.v1.endpoints.ota import router as ota_router
    api_router.include_router(ota_router, prefix="/ota", tags=["OTA升级"])
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session

router = APIRouter()


# ============================================================
# Pydantic 模型
# ============================================================

class OtaTriggerRequest(BaseModel):
    """触发 OTA 升级请求。"""

    device_id: UUID
    device_type: str = Field(..., pattern=r"^(glasses|bag_terminal)$")
    target_version: str = Field(..., max_length=50)
    from_version: str | None = None
    firmware_url: str
    firmware_sha256: str | None = Field(None, max_length=64)
    triggered_by: UUID | None = None


class OtaStatusUpdate(BaseModel):
    """OTA 状态更新请求（由挎包终端上报）。"""

    status: str = Field(..., pattern=r"^(pending|downloading|transferring|verifying|success|failed|cancelled)$")
    progress: int = Field(default=0, ge=0, le=100)
    chunks_transferred: int | None = None
    total_chunks: int | None = None
    error_message: str | None = None


class OtaUpgradeRecordResponse(BaseModel):
    """OTA 升级记录响应。"""

    id: UUID
    tenant_id: UUID
    device_id: UUID
    device_type: str
    target_version: str
    from_version: str | None
    firmware_url: str
    firmware_sha256: str | None
    status: str
    progress: int
    chunks_transferred: int
    total_chunks: int
    triggered_by: UUID | None
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AiModelVersionResponse(BaseModel):
    """AI 模型版本响应。"""

    id: UUID
    tenant_id: UUID
    model_name: str
    model_type: str
    version: str
    minio_bucket: str
    minio_object_name: str
    file_size_bytes: int | None
    sha256: str | None
    status: str
    is_active: bool
    activated_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class AiModelCreateRequest(BaseModel):
    """创建 AI 模型版本请求。"""

    tenant_id: UUID
    model_name: str = Field(..., max_length=100)
    model_type: str = Field(..., pattern=r"^(detection|embedding|llm)$")
    version: str = Field(..., max_length=50)
    minio_bucket: str = Field(..., max_length=100)
    minio_object_name: str = Field(..., max_length=500)
    file_size_bytes: int | None = None
    sha256: str | None = Field(None, max_length=64)
    input_shape: dict | None = None
    output_classes: list | None = None
    accuracy_metrics: dict | None = None


class AiModelActivateRequest(BaseModel):
    """激活 AI 模型版本请求。"""

    model_name: str
    version: str


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
# OTA 升级记录
# ============================================================

@router.get("/records", response_model=list[OtaUpgradeRecordResponse])
async def list_ota_records(
    device_id: UUID | None = None,
    device_type: str | None = None,
    status: str | None = None,
    tenant_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询 OTA 升级记录列表。"""
    conditions = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if device_id:
        conditions.append("device_id = :device_id")
        params["device_id"] = device_id
    if device_type:
        conditions.append("device_type = :device_type")
        params["device_type"] = device_type
    if status:
        conditions.append("status = :status")
        params["status"] = status
    if tenant_id:
        conditions.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id

    where_clause = " AND ".join(conditions) if conditions else "TRUE"
    result = await db.execute(
        text(f"SELECT * FROM ota_upgrade_records WHERE {where_clause} "
             f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
        params,
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


@router.get("/records/{record_id}", response_model=OtaUpgradeRecordResponse)
async def get_ota_record(
    record_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取单个 OTA 升级记录详情。"""
    result = await db.execute(
        select(text("* FROM ota_upgrade_records WHERE id = :id")),
        {"id": record_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="OTA record not found")
    return dict(row._mapping)


@router.post("/trigger", response_model=OtaUpgradeRecordResponse, status_code=201)
async def trigger_ota_upgrade(
    request: OtaTriggerRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """触发设备 OTA 升级。

    创建一条 OTA 升级记录，状态为 pending。
    挎包终端通过轮询或 WebSocket 获取待升级任务，执行升级流程。
    """
    # 校验设备存在
    if request.device_type == "glasses":
        dev = await db.execute(
            select(text("id, tenant_id, firmware_version FROM glasses_devices WHERE id = :id")),
            {"id": request.device_id},
        )
    else:
        dev = await db.execute(
            select(text("id, tenant_id FROM bag_terminals WHERE id = :id")),
            {"id": request.device_id},
        )
    device_row = dev.fetchone()
    if not device_row:
        raise HTTPException(status_code=404, detail=f"{request.device_type} device not found")

    tenant_id = device_row[1]
    from_version = request.from_version or (device_row[2] if request.device_type == "glasses" else None)

    # 检查是否有进行中的升级
    active = await db.execute(
        select(text("1 FROM ota_upgrade_records WHERE device_id = :did "
                    "AND status IN ('pending','downloading','transferring','verifying')")),
        {"did": request.device_id},
    )
    if active.fetchone():
        raise HTTPException(status_code=409, detail="An OTA upgrade is already in progress")

    record_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO ota_upgrade_records
                (id, tenant_id, device_id, device_type, target_version, from_version,
                 firmware_url, firmware_sha256, status, progress, triggered_by)
            VALUES (:id, :tenant_id, :device_id, :device_type, :target_version, :from_version,
                    :firmware_url, :firmware_sha256, 'pending', 0, :triggered_by)
        """),
        {
            "id": record_id,
            "tenant_id": tenant_id,
            "device_id": request.device_id,
            "device_type": request.device_type,
            "target_version": request.target_version,
            "from_version": from_version,
            "firmware_url": request.firmware_url,
            "firmware_sha256": request.firmware_sha256,
            "triggered_by": request.triggered_by,
        },
    )

    # 更新设备目标固件版本
    if request.device_type == "glasses":
        await db.execute(
            text("UPDATE glasses_devices SET target_firmware_version = :ver WHERE id = :did"),
            {"ver": request.target_version, "did": request.device_id},
        )

    await db.commit()

    result = await db.execute(
        select(text("* FROM ota_upgrade_records WHERE id = :id")),
        {"id": record_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.post("/records/{record_id}/status", response_model=OtaUpgradeRecordResponse)
async def update_ota_status(
    record_id: UUID,
    update: OtaStatusUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """更新 OTA 升级状态（由挎包终端上报）。

    状态流转：
        pending → downloading → transferring → verifying → success
                                        ↘ failed
                                        ↘ cancelled
    """
    result = await db.execute(
        select(text("* FROM ota_upgrade_records WHERE id = :id")),
        {"id": record_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="OTA record not found")

    set_clauses = ["status = :status", "progress = :progress"]
    params: dict[str, Any] = {
        "id": record_id,
        "status": update.status,
        "progress": update.progress,
    }
    if update.chunks_transferred is not None:
        set_clauses.append("chunks_transferred = :chunks_transferred")
        params["chunks_transferred"] = update.chunks_transferred
    if update.total_chunks is not None:
        set_clauses.append("total_chunks = :total_chunks")
        params["total_chunks"] = update.total_chunks
    if update.error_message is not None:
        set_clauses.append("error_message = :error_message")
        params["error_message"] = update.error_message

    # 完成或失败时设置 completed_at
    if update.status in ("success", "failed", "cancelled"):
        set_clauses.append("completed_at = NOW()")

    await db.execute(
        text(f"UPDATE ota_upgrade_records SET {', '.join(set_clauses)} WHERE id = :id"),
        params,
    )

    # 升级成功后更新设备固件版本
    if update.status == "success":
        record = await db.execute(
            select(text("device_id, device_type, target_version FROM ota_upgrade_records WHERE id = :id")),
            {"id": record_id},
        )
        rec_row = record.fetchone()
        if rec_row:
            if rec_row[1] == "glasses":
                await db.execute(
                    text("UPDATE glasses_devices SET firmware_version = :ver, "
                         "target_firmware_version = NULL WHERE id = :did"),
                    {"ver": rec_row[2], "did": rec_row[0]},
                )
            elif rec_row[1] == "bag_terminal":
                await db.execute(
                    text("UPDATE bag_terminals SET metadata = "
                         "jsonb_set(COALESCE(metadata, '{}'), '{firmware_version}', "
                         "to_jsonb(:ver)) WHERE id = :did"),
                    {"ver": rec_row[2], "did": rec_row[0]},
                )

    await db.commit()

    result = await db.execute(
        select(text("* FROM ota_upgrade_records WHERE id = :id")),
        {"id": record_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.post("/records/{record_id}/cancel")
async def cancel_ota_upgrade(
    record_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """取消 OTA 升级。"""
    result = await db.execute(
        text("UPDATE ota_upgrade_records SET status = 'cancelled', completed_at = NOW() "
             "WHERE id = :id AND status IN ('pending','downloading','transferring','verifying')"),
        {"id": record_id},
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=404,
            detail="Active OTA record not found or already completed",
        )
    await db.commit()
    return {"status": "cancelled"}


# ============================================================
# AI 模型版本管理
# ============================================================

@router.get("/models", response_model=list[AiModelVersionResponse])
async def list_ai_models(
    tenant_id: UUID | None = None,
    model_name: str | None = None,
    model_type: str | None = None,
    status: str | None = None,
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询 AI 模型版本列表。"""
    conditions = []
    params: dict[str, Any] = {"limit": limit}
    if tenant_id:
        conditions.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if model_name:
        conditions.append("model_name = :model_name")
        params["model_name"] = model_name
    if model_type:
        conditions.append("model_type = :model_type")
        params["model_type"] = model_type
    if status:
        conditions.append("status = :status")
        params["status"] = status
    if active_only:
        conditions.append("is_active = TRUE")

    where_clause = " AND ".join(conditions) if conditions else "TRUE"
    result = await db.execute(
        text(f"SELECT * FROM ai_model_versions WHERE {where_clause} "
             f"ORDER BY created_at DESC LIMIT :limit"),
        params,
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


@router.post("/models", response_model=AiModelVersionResponse, status_code=201)
async def create_ai_model_version(
    request: AiModelCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """注册新的 AI 模型版本。"""
    # 检查重复
    existing = await db.execute(
        select(text("1 FROM ai_model_versions WHERE tenant_id = :tid "
                    "AND model_name = :mn AND version = :ver")),
        {"tid": request.tenant_id, "mn": request.model_name, "ver": request.version},
    )
    if existing.fetchone():
        raise HTTPException(status_code=409, detail="Model version already exists")

    import json
    model_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO ai_model_versions
                (id, tenant_id, model_name, model_type, version,
                 minio_bucket, minio_object_name, file_size_bytes, sha256,
                 input_shape, output_classes, accuracy_metrics, status, is_active)
            VALUES (:id, :tenant_id, :model_name, :model_type, :version,
                    :minio_bucket, :minio_object_name, :file_size_bytes, :sha256,
                    :input_shape, :output_classes, :accuracy_metrics, 'uploaded', FALSE)
        """),
        {
            "id": model_id,
            "tenant_id": request.tenant_id,
            "model_name": request.model_name,
            "model_type": request.model_type,
            "version": request.version,
            "minio_bucket": request.minio_bucket,
            "minio_object_name": request.minio_object_name,
            "file_size_bytes": request.file_size_bytes,
            "sha256": request.sha256,
            "input_shape": json.dumps(request.input_shape) if request.input_shape else None,
            "output_classes": json.dumps(request.output_classes) if request.output_classes else None,
            "accuracy_metrics": json.dumps(request.accuracy_metrics) if request.accuracy_metrics else None,
        },
    )
    await db.commit()

    result = await db.execute(
        select(text("* FROM ai_model_versions WHERE id = :id")),
        {"id": model_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.post("/models/activate")
async def activate_ai_model(
    request: AiModelActivateRequest,
    tenant_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """激活 AI 模型版本（触发器自动取消同模型的其他版本）。

    :param tenant_id: 租户 ID
    :param request: 包含 model_name 和 version
    """
    result = await db.execute(
        text("UPDATE ai_model_versions SET is_active = TRUE, status = 'active', "
             "activated_at = NOW() "
             "WHERE tenant_id = :tid AND model_name = :mn AND version = :ver"),
        {"tid": tenant_id, "mn": request.model_name, "ver": request.version},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="AI model version not found")

    await db.commit()
    return {
        "status": "activated",
        "model_name": request.model_name,
        "version": request.version,
    }
