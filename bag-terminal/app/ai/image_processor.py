"""
图像预处理模块

流程:
    JPEG/PNG 字节 → OpenCV 解码 → Letterbox Resize (320x320) → 归一化 → NCHW → ONNX 输入

支持:
    - JPEG / PNG / BMP 解码 (OpenCV)
    - Letterbox 保持比例缩放 (填充灰边)
    - 归一化: /255.0
    - NCHW 排布: (1, 3, H, W)
    - 原始尺寸记录 (用于坐标映射)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class PreprocessResult:
    """预处理结果"""
    input_tensor: np.ndarray       # (1, 3, H, W) float32, NCHW
    original_image: np.ndarray    # 原始图像 (H, W, 3) BGR
    original_height: int          # 原始高度
    original_width: int           # 原始宽度
    input_size: int               # 模型输入尺寸
    scale: float                  # letterbox 缩放比例
    pad_x: int                    # 水平填充像素
    pad_y: int                    # 垂直填充像素
    elapsed_ms: float             # 预处理耗时 (毫秒)


class ImageProcessor:
    """
    图像预处理器

    将原始 JPEG/PNG 字节转换为 ONNX Runtime 所需的 NCHW 张量。
    使用 Letterbox 算法保持宽高比, 便于后处理坐标映射。
    """

    def __init__(self, input_size: int = 320):
        """
        Args:
            input_size: 模型输入尺寸 (正方形, 如 320 → 320x320)
        """
        self.input_size = input_size
        # YOLOv8 归一化: 255.0 (无 mean/std)
        self.normalize_scale = 1.0 / 255.0
        # Letterbox 填充颜色 (灰)
        self.pad_color = (114, 114, 114)

    def preprocess(self, image_bytes: bytes) -> PreprocessResult:
        """
        预处理图像

        Args:
            image_bytes: JPEG/PNG/BMP 图片字节数据

        Returns:
            PreprocessResult: 包含 NCHW 张量和原始尺寸信息

        Raises:
            ValueError: 图像解码失败
        """
        start = time.perf_counter()

        # 1. 解码 JPEG → BGR
        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("图像解码失败 (可能是损坏的 JPEG 数据)")

        original_height, original_width = image.shape[:2]

        # 2. Letterbox resize
        resized, scale, pad_x, pad_y = self._letterbox(
            image,
            (self.input_size, self.input_size),
        )

        # 3. BGR → RGB
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        # 4. 归一化
        normalized = rgb.astype(np.float32) * self.normalize_scale

        # 5. HWC → CHW
        transposed = np.transpose(normalized, (2, 0, 1))

        # 6. 扩展 batch 维度 → NCHW (1, 3, H, W)
        nchw = np.expand_dims(transposed, axis=0)
        nchw = np.ascontiguousarray(nchw, dtype=np.float32)

        elapsed = (time.perf_counter() - start) * 1000

        return PreprocessResult(
            input_tensor=nchw,
            original_image=image,
            original_height=original_height,
            original_width=original_width,
            input_size=self.input_size,
            scale=scale,
            pad_x=pad_x,
            pad_y=pad_y,
            elapsed_ms=elapsed,
        )

    def _letterbox(
        self,
        image: np.ndarray,
        target_size: tuple[int, int],
    ) -> tuple[np.ndarray, float, int, int]:
        """
        Letterbox 缩放: 保持宽高比, 填充灰边

        Returns:
            resized: 缩放后图像
            scale: 缩放比例
            pad_x: 水平填充 (单侧)
            pad_y: 垂直填充 (单侧)
        """
        h, w = image.shape[:2]
        target_h, target_w = target_size

        # 计算缩放比例
        scale = min(target_w / w, target_h / h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        # 缩放
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # 填充
        pad_x = (target_w - new_w) // 2
        pad_y = (target_h - new_h) // 2

        canvas = np.full(
            (target_h, target_w, 3),
            self.pad_color,
            dtype=np.uint8,
        )
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

        return canvas, scale, pad_x, pad_y

    def decode_jpeg(self, image_bytes: bytes) -> np.ndarray:
        """仅解码 JPEG, 不做预处理"""
        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("图像解码失败")
        return image

    def encode_jpeg(self, image: np.ndarray, quality: int = 95) -> bytes:
        """编码为 JPEG 字节"""
        params = [cv2.IMWRITE_JPEG_QUALITY, quality]
        encoded = cv2.imencode(".jpg", image, params)
        if not encoded[0]:
            raise ValueError("JPEG 编码失败")
        return encoded[1].tobytes()
