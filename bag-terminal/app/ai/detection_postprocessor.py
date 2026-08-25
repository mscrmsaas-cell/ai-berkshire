"""
检测结果后处理模块

功能:
    - NMS (非极大值抑制) 去除重复检测
    - 坐标映射 (320x320 → 原图尺寸, 反 Letterbox)
    - 置信度过滤
    - 检测结果序列化 (BLE 消息 payload + JSON)

YOLOv8 输出格式:
    raw_output shape: (1, 4+num_classes, num_anchors)
    - 前 4 维: cx, cy, w, h (相对于 320x320 输入)
    - 后 N 维: 各类别置信度
    - 需转置为 (num_anchors, 4+num_classes) 便于处理
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from app.ai.image_processor import PreprocessResult
from app.ai.yolo_inference import InferenceResult
from app.config import Settings
from app.models.ble_message import DetectionResultPayload

logger = structlog.get_logger(__name__)


@dataclass
class DetectionBox:
    """单个检测结果框"""
    class_id: int               # 类别 ID
    class_name: str             # 类别名称
    confidence: float           # 置信度
    x: int                      # 左上角 X (原图坐标)
    y: int                      # 左上角 Y (原图坐标)
    w: int                      # 宽度 (原图坐标)
    h: int                      # 高度 (原图坐标)
    # 320x320 坐标 (用于 BLE 传输, 节省带宽)
    x_320: int = 0
    y_320: int = 0
    w_320: int = 0
    h_320: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 4),
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
        }

    def to_ble_dict(self) -> dict[str, Any]:
        """BLE 消息格式 (320x320 坐标, 节省带宽)"""
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 3),
            "x": self.x_320,
            "y": self.y_320,
            "w": self.w_320,
            "h": self.h_320,
        }


@dataclass
class PostprocessResult:
    """后处理结果"""
    detections: list[DetectionBox] = field(default_factory=list)
    detection_count: int = 0
    defect_classes: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    model_version: str = ""
    input_size: int = 320


class DetectionPostprocessor:
    """
    检测结果后处理器

    NMS → 置信度过滤 → 坐标映射 → 序列化
    """

    def __init__(self, settings: Settings):
        """
        Args:
            settings: 全局配置 (含 confidence_threshold, nms_threshold, classes)
        """
        self.settings = settings
        self.yolo_config = settings.models.yolo
        self.confidence_threshold: float = self.yolo_config.confidence_threshold
        self.nms_threshold: float = self.yolo_config.nms_threshold
        self.classes: list[str] = self.yolo_config.classes
        self.input_size: int = self.yolo_config.input_size

    def postprocess(
        self,
        inference_result: InferenceResult,
        preprocess_result: PreprocessResult,
    ) -> PostprocessResult:
        """
        后处理 ONNX 推理输出

        Args:
            inference_result: YOLOv8n 推理输出
            preprocess_result: 预处理结果 (含原始尺寸/缩放信息)

        Returns:
            PostprocessResult: 最终检测结果
        """
        start = time.perf_counter()

        raw_output = inference_result.raw_output

        # 1. 转置 (1, 4+C, N) → (N, 4+C)
        if raw_output.ndim == 3:
            # (1, 4+C, N) → (N, 4+C)
            predictions = raw_output[0].T
        elif raw_output.ndim == 2:
            # (4+C, N) → (N, 4+C)
            predictions = raw_output.T
        else:
            predictions = raw_output

        num_anchors = predictions.shape[0]
        num_classes = len(self.classes)

        # 2. 提取 box 和 scores
        boxes_raw = predictions[:, :4]       # cx, cy, w, h
        scores_raw = predictions[:, 4:4 + num_classes]  # (N, num_classes)

        # 3. 找到每个锚点的最大类别和置信度
        max_scores = scores_raw.max(axis=1)             # (N,)
        max_class_ids = scores_raw.argmax(axis=1)        # (N,)

        # 4. 置信度过滤
        mask = max_scores >= self.confidence_threshold
        filtered_boxes = boxes_raw[mask]
        filtered_scores = max_scores[mask]
        filtered_class_ids = max_class_ids[mask]

        if len(filtered_boxes) == 0:
            elapsed = (time.perf_counter() - start) * 1000
            return PostprocessResult(
                detections=[],
                detection_count=0,
                defect_classes=[],
                elapsed_ms=elapsed,
                model_version=inference_result.model_version,
                input_size=inference_result.input_size,
            )

        # 5. 转换坐标格式 (cx,cy,w,h → x1,y1,x2,y2)
        boxes_xyxy = np.zeros_like(filtered_boxes)
        boxes_xyxy[:, 0] = filtered_boxes[:, 0] - filtered_boxes[:, 2] / 2  # x1
        boxes_xyxy[:, 1] = filtered_boxes[:, 1] - filtered_boxes[:, 3] / 2  # y1
        boxes_xyxy[:, 2] = filtered_boxes[:, 0] + filtered_boxes[:, 2] / 2  # x2
        boxes_xyxy[:, 3] = filtered_boxes[:, 1] + filtered_boxes[:, 3] / 2  # y2

        # 6. NMS
        keep_indices = self._nms(
            boxes_xyxy,
            filtered_scores,
            self.nms_threshold,
        )

        # 7. 构建检测结果
        detections: list[DetectionBox] = []
        for idx in keep_indices:
            class_id = int(filtered_class_ids[idx])
            confidence = float(filtered_scores[idx])

            # 320x320 坐标 (裁剪到边界内)
            x1_320, y1_320, x2_320, y2_320 = boxes_xyxy[idx]
            x1_320 = max(0, min(self.input_size - 1, int(x1_320)))
            y1_320 = max(0, min(self.input_size - 1, int(y1_320)))
            x2_320 = max(0, min(self.input_size - 1, int(x2_320)))
            y2_320 = max(0, min(self.input_size - 1, int(y2_320)))

            w_320 = x2_320 - x1_320
            h_320 = y2_320 - y1_320

            if w_320 <= 0 or h_320 <= 0:
                continue

            # 映射到原图坐标 (反 Letterbox)
            scale = preprocess_result.scale
            pad_x = preprocess_result.pad_x
            pad_y = preprocess_result.pad_y

            x1_orig = int((x1_320 - pad_x) / scale)
            y1_orig = int((y1_320 - pad_y) / scale)
            x2_orig = int((x2_320 - pad_x) / scale)
            y2_orig = int((y2_320 - pad_y) / scale)

            # 裁剪到原图边界
            x1_orig = max(0, min(preprocess_result.original_width - 1, x1_orig))
            y1_orig = max(0, min(preprocess_result.original_height - 1, y1_orig))
            x2_orig = max(0, min(preprocess_result.original_width - 1, x2_orig))
            y2_orig = max(0, min(preprocess_result.original_height - 1, y2_orig))

            w_orig = x2_orig - x1_orig
            h_orig = y2_orig - y1_orig

            if w_orig <= 0 or h_orig <= 0:
                continue

            class_name = self.classes[class_id] if class_id < len(self.classes) else f"unknown_{class_id}"

            detections.append(DetectionBox(
                class_id=class_id,
                class_name=class_name,
                confidence=confidence,
                x=x1_orig,
                y=y1_orig,
                w=w_orig,
                h=h_orig,
                x_320=x1_320,
                y_320=y1_320,
                w_320=w_320,
                h_320=h_320,
            ))

        elapsed = (time.perf_counter() - start) * 1000

        defect_classes = list(set(d.class_name for d in detections))

        return PostprocessResult(
            detections=detections,
            detection_count=len(detections),
            defect_classes=defect_classes,
            elapsed_ms=elapsed,
            model_version=inference_result.model_version,
            input_size=inference_result.input_size,
        )

    def _nms(
        self,
        boxes: np.ndarray,
        scores: np.ndarray,
        iou_threshold: float,
    ) -> list[int]:
        """
        NMS (非极大值抑制)

        使用 IoU 阈值去除重叠框, 保留置信度最高的框。

        Args:
            boxes: (N, 4) xyxy 格式
            scores: (N,) 置信度
            iou_threshold: IoU 阈值

        Returns:
            保留的索引列表
        """
        # 按置信度排序 (降序)
        order = scores.argsort()[::-1]
        keep: list[int] = []

        while len(order) > 0:
            # 取最高分
            idx = order[0]
            keep.append(int(idx))

            if len(order) == 1:
                break

            # 计算与其余框的 IoU
            rest = order[1:]
            ious = self._compute_iou(
                boxes[idx],
                boxes[rest],
            )

            # 保留 IoU < 阈值的框
            mask = ious < iou_threshold
            order = rest[mask]

        return keep

    def _compute_iou(self, box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
        """
        计算 IoU (Intersection over Union)

        Args:
            box: (4,) 单个框 xyxy
            boxes: (N, 4) 多个框 xyxy

        Returns:
            (N,) IoU 值
        """
        # 交集
        x1 = np.maximum(box[0], boxes[:, 0])
        y1 = np.maximum(box[1], boxes[:, 1])
        x2 = np.minimum(box[2], boxes[:, 2])
        y2 = np.minimum(box[3], boxes[:, 3])

        inter_w = np.maximum(0, x2 - x1)
        inter_h = np.maximum(0, y2 - y1)
        inter_area = inter_w * inter_h

        # 各框面积
        box_area = (box[2] - box[0]) * (box[3] - box[1])
        boxes_area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])

        # 并集
        union_area = box_area + boxes_area - inter_area

        iou = inter_area / np.maximum(union_area, 1e-6)
        return iou

    # -------------------------------------------------------------------
    # 序列化
    # -------------------------------------------------------------------

    def serialize_for_ble(
        self,
        result: PostprocessResult,
        frame_index: int = 0,
        inference_time_ms: int = 0,
    ) -> bytes:
        """
        序列化检测结果为 BLE 消息 payload

        使用 DetectionResultPayload 模型序列化, 包含 JSON 格式的检测列表。
        """
        payload = DetectionResultPayload(
            frame_index=frame_index,
            detection_count=result.detection_count,
            detections=[d.to_ble_dict() for d in result.detections],
            inference_time_ms=inference_time_ms,
        )
        return payload.to_bytes()

    def serialize_to_json(self, result: PostprocessResult) -> str:
        """序列化为 JSON 字符串 (用于 API 响应/存储)"""
        return json.dumps(
            {
                "detections": [d.to_dict() for d in result.detections],
                "detection_count": result.detection_count,
                "defect_classes": result.defect_classes,
                "model_version": result.model_version,
                "input_size": result.input_size,
                "postprocess_ms": round(result.elapsed_ms, 2),
            },
            ensure_ascii=False,
        )

    def serialize_to_dict(self, result: PostprocessResult) -> dict[str, Any]:
        """序列化为字典 (用于 Pydantic 模型)"""
        return {
            "detections": [d.to_dict() for d in result.detections],
            "detection_count": result.detection_count,
            "defect_classes": result.defect_classes,
            "model_version": result.model_version,
            "input_size": result.input_size,
            "postprocess_ms": round(result.elapsed_ms, 2),
        }
