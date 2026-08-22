"""
巡检记录模型

定义:
    - InspectionTask: 巡检任务 (从 SaaS 同步, 含同步状态)
    - InspectionPhoto: 巡检照片 (本地缓存 + MinIO 上传状态)
    - DetectionResult: AI 检测结果
    - Alert: 告警记录 (最高同步优先级)
    - AlertSeverity: 告警严重度枚举
    - SyncStatus: 同步状态枚举
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class AlertSeverity(IntEnum):
    """告警严重度"""
    INFO = 0       # 提示
    LOW = 1        # 低
    MEDIUM = 2     # 中
    HIGH = 3       # 高
    CRITICAL = 4   # 紧急


class SyncStatus(str, Enum):
    """同步状态"""
    PENDING = "pending"          # 待同步
    SYNCING = "syncing"           # 同步中
    SYNCED = "synced"             # 已同步
    FAILED = "failed"             # 同步失败
    SKIPPED = "skipped"           # 跳过 (无需同步)


class InspectionTask(BaseModel):
    """
    巡检任务

    从 SaaS 后端同步到本地 SQLite 缓存, 离线可执行。
    """
    # --- 主键 ---
    id: str = Field(default_factory=lambda: str(uuid4()), description="任务 ID (本地生成)")
    saas_task_id: str | None = Field(default=None, description="SaaS 后端任务 ID")
    task_code: str = Field(default="", description="任务编号")

    # --- 关联 ---
    project_id: str = Field(description="项目 ID")
    project_name: str = Field(default="", description="项目名称")
    route_name: str = Field(default="", description="线路名称")
    route_id: str = Field(default="", description="线路 ID")
    worker_id: str = Field(description="巡检员 ID")
    worker_name: str = Field(default="", description="巡检员姓名")

    # --- 任务信息 ---
    title: str = Field(default="", description="任务标题")
    description: str = Field(default="", description="任务描述")
    inspection_items: list[dict[str, Any]] = Field(
        default_factory=list, description="巡检项目清单"
    )

    # --- 时间 ---
    scheduled_start: datetime | None = Field(default=None, description="计划开始时间")
    scheduled_end: datetime | None = Field(default=None, description="计划结束时间")
    actual_start: datetime | None = Field(default=None, description="实际开始时间")
    actual_end: datetime | None = Field(default=None, description="实际结束时间")

    # --- 统计 ---
    photo_count: int = Field(default=0, description="已拍照数")
    alert_count: int = Field(default=0, description="告警数")
    detection_count: int = Field(default=0, description="检测次数")
    distance_meters: float = Field(default=0.0, description="巡检距离 (米)")

    # --- 状态 ---
    status: str = Field(default="pending", description="任务状态 (pending/in_progress/completed/cancelled)")
    sync_status: SyncStatus = Field(default=SyncStatus.PENDING, description="同步状态")
    synced_to_cloud: bool = Field(default=False, description="是否已同步到云")
    retry_count: int = Field(default=0, description="同步重试次数")

    # --- 元数据 ---
    glasses_device_id: str | None = Field(default=None, description="执行巡检的眼镜 ID")
    bag_terminal_id: str | None = Field(default=None, description="挎包终端 ID")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def mark_started(self) -> None:
        """标记任务开始"""
        self.status = "in_progress"
        self.actual_start = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def mark_completed(self) -> None:
        """标记任务完成"""
        self.status = "completed"
        self.actual_end = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def to_saas_dict(self) -> dict[str, Any]:
        """转换为 SaaS API 上传格式"""
        return {
            "saas_task_id": self.saas_task_id,
            "project_id": self.project_id,
            "worker_id": self.worker_id,
            "status": self.status,
            "actual_start": self.actual_start.isoformat() if self.actual_start else None,
            "actual_end": self.actual_end.isoformat() if self.actual_end else None,
            "photo_count": self.photo_count,
            "alert_count": self.alert_count,
            "detection_count": self.detection_count,
            "distance_meters": self.distance_meters,
            "glasses_device_id": self.glasses_device_id,
        }


class InspectionPhoto(BaseModel):
    """
    巡检照片缓存

    照片先存本地, 网络恢复后按优先级上传到 MinIO。
    """
    id: str = Field(default_factory=lambda: str(uuid4()), description="照片 ID")
    task_id: str = Field(description="巡检任务 ID")
    photo_index: int = Field(description="照片序号")

    # --- 文件 ---
    local_path: str = Field(description="本地文件路径")
    file_size: int = Field(default=0, description="文件大小 (字节)")
    file_hash: str = Field(default="", description="文件 SHA-256")
    minio_object_name: str | None = Field(default=None, description="MinIO 对象名")

    # --- 元数据 ---
    capture_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gps_lat: float = Field(default=0.0, description="纬度")
    gps_lon: float = Field(default=0.0, description="经度")
    gps_accuracy: float = Field(default=0.0, description="定位精度 (米)")
    glasses_device_id: str | None = Field(default=None, description="拍照眼镜 ID")

    # --- 检测关联 ---
    detection_result_id: str | None = Field(default=None, description="关联检测结果 ID")
    has_defects: bool = Field(default=False, description="是否检测到缺陷")

    # --- 同步状态 ---
    upload_status: SyncStatus = Field(default=SyncStatus.PENDING, description="上传状态")
    retry_count: int = Field(default=0, description="上传重试次数")
    uploaded_at: datetime | None = Field(default=None, description="上传时间")

    # --- 时间 ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def sync_priority(self) -> int:
        """同步优先级 (数字越小优先级越高)"""
        # 有缺陷的照片优先上传
        if self.has_defects:
            return 1
        return 2

    def to_saas_dict(self) -> dict[str, Any]:
        """转换为 SaaS API 上传格式"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "photo_index": self.photo_index,
            "minio_object_name": self.minio_object_name,
            "capture_time": self.capture_time.isoformat(),
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "glasses_device_id": self.glasses_device_id,
            "has_defects": self.has_defects,
        }


class DetectionResult(BaseModel):
    """
    AI 检测结果

    每张照片的 YOLOv8n 推理结果, 含 8 类铁路缺陷。
    """
    id: str = Field(default_factory=lambda: str(uuid4()), description="检测结果 ID")
    photo_id: str | None = Field(default=None, description="关联照片 ID")
    task_id: str | None = Field(default=None, description="关联巡检任务 ID")
    glasses_device_id: str | None = Field(default=None, description="眼镜设备 ID")

    # --- 模型信息 ---
    model_name: str = Field(default="yolov8n_railway", description="模型名称")
    model_version: str = Field(default="", description="模型版本")
    input_size: int = Field(default=320, description="输入尺寸")

    # --- 检测结果 ---
    detections: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "检测结果列表: "
            "[{class_id, class_name, confidence, x, y, w, h}]"
            "坐标为原图像素值"
        )
    )
    detection_count: int = Field(default=0, description="检测到的缺陷总数")
    defect_classes: list[str] = Field(default_factory=list, description="检测到的缺陷类别列表")

    # --- 性能 ---
    inference_time_ms: float = Field(default=0.0, description="推理耗时 (毫秒)")
    preprocessor_time_ms: float = Field(default=0.0, description="预处理耗时 (毫秒)")
    postprocessor_time_ms: float = Field(default=0.0, description="后处理耗时 (毫秒)")

    # --- 同步 ---
    sync_status: SyncStatus = Field(default=SyncStatus.PENDING)
    retry_count: int = Field(default=0)

    # --- 时间 ---
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_saas_dict(self) -> dict[str, Any]:
        """转换为 SaaS API 上传格式"""
        return {
            "id": self.id,
            "photo_id": self.photo_id,
            "task_id": self.task_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "detections": self.detections,
            "detection_count": self.detection_count,
            "defect_classes": self.defect_classes,
            "inference_time_ms": self.inference_time_ms,
            "detected_at": self.detected_at.isoformat(),
        }


class Alert(BaseModel):
    """
    告警记录

    同步优先级最高 (priority=0), 网络恢复后优先上报。
    """
    id: str = Field(default_factory=lambda: str(uuid4()), description="告警 ID")
    task_id: str | None = Field(default=None, description="关联巡检任务 ID")
    glasses_device_id: str | None = Field(default=None, description="眼镜设备 ID")

    # --- 告警信息 ---
    alert_type: str = Field(description="告警类型 (YOLO 类别名)")
    alert_type_id: int = Field(description="告警类型 ID")
    severity: AlertSeverity = Field(default=AlertSeverity.MEDIUM, description="严重度")
    message: str = Field(default="", description="告警描述")
    description: str = Field(default="", description="详细描述")

    # --- 位置 ---
    gps_lat: float = Field(default=0.0, description="纬度")
    gps_lon: float = Field(default=0.0, description="经度")
    gps_accuracy: float = Field(default=0.0, description="定位精度 (米)")
    kilometer_mark: str = Field(default="", description="公里标")

    # --- 关联 ---
    photo_id: str | None = Field(default=None, description="截图照片 ID")
    screenshot_path: str | None = Field(default=None, description="截图本地路径")
    detection_result_id: str | None = Field(default=None, description="关联检测结果 ID")

    # --- 同步 (最高优先级) ---
    sync_status: SyncStatus = Field(default=SyncStatus.PENDING)
    sync_priority: int = Field(default=0, description="同步优先级 (0=最高)")
    retry_count: int = Field(default=0)
    acknowledged: bool = Field(default=False, description="是否已确认")
    acknowledged_by: str | None = Field(default=None, description="确认人")
    acknowledged_at: datetime | None = Field(default=None, description="确认时间")

    # --- 时间 ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_saas_dict(self) -> dict[str, Any]:
        """转换为 SaaS API 上传格式"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "alert_type": self.alert_type,
            "alert_type_id": self.alert_type_id,
            "severity": self.severity.value,
            "severity_name": self.severity.name,
            "message": self.message,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "kilometer_mark": self.kilometer_mark,
            "photo_id": self.photo_id,
            "detection_result_id": self.detection_result_id,
            "created_at": self.created_at.isoformat(),
        }
