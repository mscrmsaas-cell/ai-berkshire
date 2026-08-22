"""
AI 推理包 — YOLOv8n ONNX Runtime 铁路缺陷检测

子模块:
    image_processor         — 图像预处理 (JPEG 解码 → resize 320x320 → 归一化 → NCHW)
    yolo_inference           — ONNX Runtime YOLOv8n 推理
    detection_postprocessor — 后处理 (NMS, 坐标映射, BLE 消息序列化)
    model_manager           — 模型版本管理 (加载/卸载, 热更新, 回滚)
"""

from app.ai.image_processor import ImageProcessor, PreprocessResult
from app.ai.yolo_inference import YoloInference, InferenceResult
from app.ai.detection_postprocessor import (
    DetectionPostprocessor,
    DetectionBox,
    PostprocessResult,
)
from app.ai.model_manager import ModelManager

__all__ = [
    "ImageProcessor",
    "PreprocessResult",
    "YoloInference",
    "InferenceResult",
    "DetectionPostprocessor",
    "DetectionBox",
    "PostprocessResult",
    "ModelManager",
]
