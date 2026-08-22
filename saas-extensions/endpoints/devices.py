"""设备管理 API — 智能眼镜与挎包终端 CRUD + 绑定管理。

FastAPI Router，对应 PostgreSQL 表：
  - glasses_devices
  - bag_terminals
  - glasses_bag_binding

注册方式::

    from app.api.v1.endpoints.devices import router as devices_router
    api_router.include_router(devices_router, prefix="/devices", tags=["设备管理"])
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session

router = APIRouter()


# ============================================================
# Pydantic 请求/响应模型
# ============================================================

class GlassesDeviceCreate(BaseModel):
    """创建眼镜设备请求。"""

    tenant_id: UUID
    tuya_device_id: str | None = None
    ble_address: str = Field(..., max_length=20)
    serial_number: str | None = None
    pid: str | None = None
    name: str | None = None
    firmware_version: str = "1.0.0"
    assigned_worker_id: UUID | None = None
    assigned_project_id: UUID | None = None


class GlassesDeviceUpdate(BaseModel):
    """更新眼镜设备请求。"""

    name: str | None = None
    firmware_version: str | None = None
    assigned_worker_id: UUID | None = None
    assigned_project_id: UUID | None = None
    status: str | None = Field(None, pattern=r"^(registered|online|offline|maintenance|retired)$")
    mode: str | None = Field(None, pattern=r"^(standby|inspection|alert|charging|sleeping)$")


class GlassesDeviceResponse(BaseModel):
    """眼镜设备响应。"""

    id: UUID
    tenant_id: UUID
    tuya_device_id: str | None
    ble_address: str
    serial_number: str | None
    name: str | None
    firmware_version: str
    status: str
    mode: str
    battery_level: int
    is_charging: bool
    camera_attached: bool
    ble_rssi: int
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BagTerminalCreate(BaseModel):
    """创建挎包终端请求。"""

    tenant_id: UUID
    device_id: str = Field(..., max_length=100)
    serial_number: str | None = None
    tuya_gateway_id: str | None = None
    sim_iccid: str | None = None
    sim_imei: str | None = None
    hostname: str | None = None
    location: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    max_glasses: int = Field(default=4, ge=1, le=16)


class BagTerminalResponse(BaseModel):
    """挎包终端响应。"""

    id: UUID
    tenant_id: UUID
    device_id: str
    serial_number: str | None
    tuya_gateway_id: str | None
    sim_iccid: str | None
    sim_imei: str | None
    hostname: str | None
    location: str | None
    max_glasses: int
    connected_glasses_count: int
    status: str
    cpu_usage: float
    memory_usage: float
    temperature_c: float
    last_heartbeat: datetime | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BindingCreate(BaseModel):
    """创建绑定请求。"""

    glasses_device_id: UUID
    bag_terminal_id: UUID
    binding_type: str = Field(default="auto", pattern=r"^(auto|manual)$")


class BindingResponse(BaseModel):
    """绑定响应。"""

    id: UUID
    tenant_id: UUID
    glasses_device_id: UUID
    bag_terminal_id: UUID
    binding_type: str
    status: str
    bound_at: datetime
    released_at: datetime | None

    class Config:
        from_attributes = True


# ============================================================
# 数据库依赖
# ============================================================

async def get_db():
    """获取数据库会话。"""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


# ============================================================
# 眼镜设备管理
# ============================================================

@router.get("/glasses", response_model=list[GlassesDeviceResponse])
async def list_glasses_devices(
    tenant_id: UUID | None = None,
    status: str | None = None,
    project_id: UUID | None = None,
    worker_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """列出智能眼镜设备。"""
    # 直接 SQL 查询（因为表是扩展 SQL 创建的，没有 ORM 模型）
    conditions = []
    params: dict[str, Any] = {}
    if tenant_id:
        conditions.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if status:
        conditions.append("status = :status")
        params["status"] = status
    if project_id:
        conditions.append("assigned_project_id = :project_id")
        params["project_id"] = project_id
    if worker_id:
        conditions.append("assigned_worker_id = :worker_id")
        params["worker_id"] = worker_id

    where_clause = " AND ".join(conditions) if conditions else "TRUE"
    query = (
        select(text(f"* FROM glasses_devices WHERE {where_clause} "
                    f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"))
    )
    params["limit"] = limit
    params["offset"] = offset
    result = await db.execute(query, params)
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


@router.post("/glasses", response_model=GlassesDeviceResponse, status_code=201)
async def create_glasses_device(
    device: GlassesDeviceCreate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """注册新的智能眼镜设备。"""
    # 检查 BLE 地址唯一
    exists = await db.execute(
        select(text("1 FROM glasses_devices WHERE ble_address = :ble_addr")),
        {"ble_addr": device.ble_address},
    )
    if exists.fetchone():
        raise HTTPException(status_code=409, detail="BLE address already registered")

    # 检查涂鸦设备 ID 唯一（如提供）
    if device.tuya_device_id:
        exists = await db.execute(
            select(text("1 FROM glasses_devices WHERE tuya_device_id = :tuya_id")),
            {"tuya_id": device.tuya_device_id},
        )
        if exists.fetchone():
            raise HTTPException(status_code=409, detail="Tuya device ID already registered")

    device_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO glasses_devices
                (id, tenant_id, tuya_device_id, ble_address, serial_number,
                 pid, name, firmware_version, assigned_worker_id,
                 assigned_project_id, status, mode)
            VALUES (:id, :tenant_id, :tuya_device_id, :ble_address, :serial_number,
                    :pid, :name, :firmware_version, :assigned_worker_id,
                    :assigned_project_id, 'registered', 'standby')
        """),
        {
            "id": device_id,
            "tenant_id": device.tenant_id,
            "tuya_device_id": device.tuya_device_id,
            "ble_address": device.ble_address,
            "serial_number": device.serial_number,
            "pid": device.pid,
            "name": device.name,
            "firmware_version": device.firmware_version,
            "assigned_worker_id": device.assigned_worker_id,
            "assigned_project_id": device.assigned_project_id,
        },
    )
    await db.commit()

    result = await db.execute(
        select(text("* FROM glasses_devices WHERE id = :id")),
        {"id": device_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.get("/glasses/{device_id}", response_model=GlassesDeviceResponse)
async def get_glasses_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取眼镜设备详情。"""
    result = await db.execute(
        select(text("* FROM glasses_devices WHERE id = :id")),
        {"id": device_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Glasses device not found")
    return dict(row._mapping)


@router.patch("/glasses/{device_id}", response_model=GlassesDeviceResponse)
async def update_glasses_device(
    device_id: UUID,
    updates: GlassesDeviceUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """更新眼镜设备信息。"""
    # 检查存在
    exists = await db.execute(
        select(text("1 FROM glasses_devices WHERE id = :id")),
        {"id": device_id},
    )
    if not exists.fetchone():
        raise HTTPException(status_code=404, detail="Glasses device not found")

    set_clauses = []
    params: dict[str, Any] = {"id": device_id}
    for field_name, value in updates.model_dump(exclude_unset=True).items():
        if value is not None:
            set_clauses.append(f"{field_name} = :{field_name}")
            params[field_name] = value

    if not set_clauses:
        raise HTTPException(status_code=400, detail="No fields to update")

    await db.execute(
        text(f"UPDATE glasses_devices SET {', '.join(set_clauses)} WHERE id = :id"),
        params,
    )
    await db.commit()

    result = await db.execute(
        select(text("* FROM glasses_devices WHERE id = :id")),
        {"id": device_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.delete("/glasses/{device_id}", status_code=204)
async def delete_glasses_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """注销眼镜设备（设为 retired 状态）。"""
    result = await db.execute(
        text("UPDATE glasses_devices SET status = 'retired' WHERE id = :id"),
        {"id": device_id},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Glasses device not found")
    await db.commit()


# ============================================================
# 挎包终端管理
# ============================================================

@router.get("/bag-terminals", response_model=list[BagTerminalResponse])
async def list_bag_terminals(
    tenant_id: UUID | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """列出挎包终端。"""
    conditions = []
    params: dict[str, Any] = {}
    if tenant_id:
        conditions.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if status:
        conditions.append("status = :status")
        params["status"] = status
    where_clause = " AND ".join(conditions) if conditions else "TRUE"
    result = await db.execute(
        text(f"SELECT * FROM bag_terminals WHERE {where_clause} "
             f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
        {**params, "limit": limit, "offset": offset},
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


@router.post("/bag-terminals", response_model=BagTerminalResponse, status_code=201)
async def create_bag_terminal(
    terminal: BagTerminalCreate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """注册新的挎包终端。"""
    exists = await db.execute(
        select(text("1 FROM bag_terminals WHERE device_id = :device_id")),
        {"device_id": terminal.device_id},
    )
    if exists.fetchone():
        raise HTTPException(status_code=409, detail="Device ID already registered")

    terminal_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO bag_terminals
                (id, tenant_id, device_id, serial_number, tuya_gateway_id,
                 sim_iccid, sim_imei, hostname, location, longitude, latitude,
                 max_glasses, status)
            VALUES (:id, :tenant_id, :device_id, :serial_number, :tuya_gateway_id,
                    :sim_iccid, :sim_imei, :hostname, :location, :longitude, :latitude,
                    :max_glasses, 'registered')
        """),
        {
            "id": terminal_id,
            "tenant_id": terminal.tenant_id,
            "device_id": terminal.device_id,
            "serial_number": terminal.serial_number,
            "tuya_gateway_id": terminal.tuya_gateway_id,
            "sim_iccid": terminal.sim_iccid,
            "sim_imei": terminal.sim_imei,
            "hostname": terminal.hostname,
            "location": terminal.location,
            "longitude": terminal.longitude,
            "latitude": terminal.latitude,
            "max_glasses": terminal.max_glasses,
        },
    )
    await db.commit()

    result = await db.execute(
        select(text("* FROM bag_terminals WHERE id = :id")),
        {"id": terminal_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.get("/bag-terminals/{terminal_id}", response_model=BagTerminalResponse)
async def get_bag_terminal(
    terminal_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取挎包终端详情。"""
    result = await db.execute(
        select(text("* FROM bag_terminals WHERE id = :id")),
        {"id": terminal_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Bag terminal not found")
    return dict(row._mapping)


@router.patch("/bag-terminals/{terminal_id}")
async def update_bag_terminal_heartbeat(
    terminal_id: UUID,
    cpu_usage: float | None = None,
    memory_usage: float | None = None,
    temperature_c: float | None = None,
    connected_glasses: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """更新挎包终端心跳状态。"""
    set_clauses = ["last_heartbeat = NOW()", "status = 'online'"]
    params: dict[str, Any] = {"id": terminal_id}
    if cpu_usage is not None:
        set_clauses.append("cpu_usage = :cpu_usage")
        params["cpu_usage"] = cpu_usage
    if memory_usage is not None:
        set_clauses.append("memory_usage = :memory_usage")
        params["memory_usage"] = memory_usage
    if temperature_c is not None:
        set_clauses.append("temperature_c = :temperature_c")
        params["temperature_c"] = temperature_c
    if connected_glasses is not None:
        set_clauses.append("connected_glasses_count = :connected_glasses")
        params["connected_glasses"] = connected_glasses

    result = await db.execute(
        text(f"UPDATE bag_terminals SET {', '.join(set_clauses)} WHERE id = :id"),
        params,
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Bag terminal not found")
    await db.commit()
    return {"status": "heartbeat_updated"}


# ============================================================
# 眼镜-挎包绑定管理
# ============================================================

@router.get("/bindings", response_model=list[BindingResponse])
async def list_bindings(
    glasses_device_id: UUID | None = None,
    bag_terminal_id: UUID | None = None,
    status: str = "active",
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """列出眼镜-挎包绑定关系。"""
    conditions = ["status = :status"]
    params: dict[str, Any] = {"status": status, "limit": limit}
    if glasses_device_id:
        conditions.append("glasses_device_id = :glasses_device_id")
        params["glasses_device_id"] = glasses_device_id
    if bag_terminal_id:
        conditions.append("bag_terminal_id = :bag_terminal_id")
        params["bag_terminal_id"] = bag_terminal_id

    result = await db.execute(
        text(f"SELECT * FROM glasses_bag_binding WHERE {' AND '.join(conditions)} "
             f"ORDER BY bound_at DESC LIMIT :limit"),
        params,
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


@router.post("/bindings", response_model=BindingResponse, status_code=201)
async def create_binding(
    binding: BindingCreate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """创建眼镜-挎包绑定。"""
    # 检查眼镜设备存在
    glasses = await db.execute(
        select(text("id, tenant_id FROM glasses_devices WHERE id = :id")),
        {"id": binding.glasses_device_id},
    )
    glasses_row = glasses.fetchone()
    if not glasses_row:
        raise HTTPException(status_code=404, detail="Glasses device not found")

    # 检查终端存在
    terminal = await db.execute(
        select(text("id, max_glasses, connected_glasses_count FROM bag_terminals WHERE id = :id")),
        {"id": binding.bag_terminal_id},
    )
    terminal_row = terminal.fetchone()
    if not terminal_row:
        raise HTTPException(status_code=404, detail="Bag terminal not found")

    # 检查终端容量
    if terminal_row[2] >= terminal_row[1]:
        raise HTTPException(status_code=409, detail="Bag terminal at max capacity")

    # 检查是否已有 active 绑定
    existing = await db.execute(
        select(text("1 FROM glasses_bag_binding WHERE glasses_device_id = :gid "
                    "AND status = 'active'")),
        {"gid": binding.glasses_device_id},
    )
    if existing.fetchone():
        raise HTTPException(status_code=409, detail="Glasses already bound to a terminal")

    binding_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO glasses_bag_binding
                (id, tenant_id, glasses_device_id, bag_terminal_id, binding_type, status)
            VALUES (:id, :tenant_id, :glasses_device_id, :bag_terminal_id, :binding_type, 'active')
        """),
        {
            "id": binding_id,
            "tenant_id": glasses_row[1],
            "glasses_device_id": binding.glasses_device_id,
            "bag_terminal_id": binding.bag_terminal_id,
            "binding_type": binding.binding_type,
        },
    )
    await db.commit()

    result = await db.execute(
        select(text("* FROM glasses_bag_binding WHERE id = :id")),
        {"id": binding_id},
    )
    row = result.fetchone()
    return dict(row._mapping)


@router.post("/bindings/{binding_id}/release", response_model=BindingResponse)
async def release_binding(
    binding_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """释放绑定。"""
    result = await db.execute(
        text("UPDATE glasses_bag_binding SET status = 'released', released_at = NOW() "
             "WHERE id = :id AND status = 'active'"),
        {"id": binding_id},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Active binding not found")

    await db.commit()
    result = await db.execute(
        select(text("* FROM glasses_bag_binding WHERE id = :id")),
        {"id": binding_id},
    )
    row = result.fetchone()
    return dict(row._mapping)
