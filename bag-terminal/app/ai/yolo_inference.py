"""
YOLOv8n ONNX Runtime 推理模块

功能:
    - 加载 YOLOv8n ONNX 模型 (ARM NEON 优化)
    - 异步推理 (在线程池中运行, 不阻塞事件循环)
    - 8 类铁路缺陷检测

YOLOv8n 输出格式:
    output shape: (1, 84, 8400) 或 (1, num_classes+4, num_anchors)
    - 84 = 4 (cx, cy, w, h) + 80 (COCO classes)
    - 铁路定制模型: (1, 12, 8400) → 4 + 8 classes
    - 8400 = 320/8 * 320/8 + 320/16 * 320/16 + 320/32 * 320/32 (多尺度锚点)

注意: 此模块仅负责推理, 后处理 (NMS/坐标映射) 在 detection_postprocessor.py
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
import structlog

from app.ai.image_processor import PreprocessResult
from app.config import Settings

logger = structlog.get_logger(__name__)


@dataclass
class InferenceResult:
    """推理结果"""
    raw_output: np.ndarray        # 原始 ONNX 输出张量
    input_size: int               # 输入尺寸
    elapsed_ms: float             # 推理耗时 (毫秒)
    model_version: str            # 模型版本


class YoloInference:
    """
    YOLOv8n ONNX 推理器

    使用 ONNX Runtime 的 CPU EP (ARM NEON 优化)。
    """

    # YOLOv8n 默认输入名
    DEFAULT_INPUT_NAME = "images"
    # YOLOv8n 默认输出名
    DEFAULT_OUTPUT_NAME = "output0"

    def __init__(self, settings: Settings):
        """
        Args:
            settings: 全局配置
        """
        self.settings = settings
        self.yolo_config = settings.models.yolo

        self._session: ort.InferenceSession | None = None
        self._input_name: str = self.DEFAULT_INPUT_NAME
        self._output_name: str = self.DEFAULT_OUTPUT_NAME
        self._input_size: int = self.yolo_config.input_size
        self._model_version: str = "unknown"
        self._is_loaded: bool = False

        # ONNX Runtime 会话选项
        self._session_options = self._create_session_options()

    def _create_session_options(self) -> ort.SessionOptions:
        """创建 ONNX Runtime 会话选项 (ARM 优化)"""
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # ARM NEON 优化
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # 内存优化
        opts.enable_cpu_mem_arena = True
        opts.enable_mem_pattern = True
        return opts

    # -------------------------------------------------------------------
    # 模型加载
    # -------------------------------------------------------------------

    async def load_model(self, model_path: str, version: str = "1.0.0") -> None:
        """
        异步加载 ONNX 模型 (在线程池中执行, 避免阻塞事件循环)

        Args:
            model_path: ONNX 模型文件路径
            version: 模型版本号
        """
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        logger.info("yolo.loading_model", path=model_path, version=version)

        # 在线程池中加载 (避免阻塞)
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._load_model_sync,
            str(path),
        )

        self._model_version = version
        self._is_loaded = True
        logger.info(
            "yolo.model_loaded",
            version=version,
            input_name=self._input_name,
            output_name=self._output_name,
            input_size=self._input_size,
        )

    def _load_model_sync(self, model_path: str) -> None:
        """同步加载模型"""
        # 使用 CPU Execution Provider (ARM NEON)
        providers = [
            "CPUExecutionProvider",
        ]

        self._session = ort.InferenceSession(
            model_path,
            sess_options=self._session_options,
            providers=providers,
        )

        # 获取输入/输出名称
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()

        if inputs:
            self._input_name = inputs[0].name
            # 从输入 shape 推断输入尺寸
            input_shape = inputs[0].shape
            if len(input_shape) == 4 and input_shape[2] is not None:
                self._input_size = int(input_shape[2])

        if outputs:
            self._output_name = outputs[0].name

    async def unload_model(self) -> None:
        """卸载模型, 释放内存"""
        if self._session:
            # ONNX Runtime 的 session 需要 del 来释放
            del self._session
            self._session = None
        self._is_loaded = False
        logger.info("yolo.model_unloaded")

    # -------------------------------------------------------------------
    # 推理
    # -------------------------------------------------------------------

    async def infer(self, preprocess_result: PreprocessResult) -> InferenceResult:
        """
        异步推理

        Args:
            preprocess_result: 图像预处理结果 (NCHW 张量)

        Returns:
            InferenceResult: 原始输出 + 耗时信息
        """
        if not self._is_loaded or not self._session:
            raise RuntimeError("模型未加载, 请先调用 load_model()")

        # 在线程池中执行推理 (避免阻塞事件循环)
        start = time.perf_counter()

        raw_output = await asyncio.get_event_loop().run_in_executor(
            None,
            self._infer_sync,
            preprocess_result.input_tensor,
        )

        elapsed = (time.perf_counter() - start) * 1000

        return InferenceResult(
            raw_output=raw_output,
            input_size=self._input_size,
            elapsed_ms=elapsed,
            model_version=self._model_version,
        )

    def _infer_sync(self, input_tensor: np.ndarray) -> np.ndarray:
        """同步推理 (在线程池中调用)"""
        outputs = self._session.run(
            [self._output_name],
            {self._input_name: input_tensor},
        )
        return outputs[0]

    async def infer_batch(
        self,
        input_tensors: list[np.ndarray],
    ) -> list[np.ndarray]:
        """批量推理 (拼接 batch 维度)"""
        if not input_tensors:
            return []

        batch = np.concatenate(input_tensors, axis=0)
        raw_output = await asyncio.get_event_loop().run_in_executor(
            None,
            self._infer_sync,
            batch,
        )
        # 分割结果
        batch_size = len(input_tensors)
        return [raw_output[i:i + 1] for i in range(batch_size)]

    # -------------------------------------------------------------------
    # 状态查询
    # -------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def input_size(self) -> int:
        return self._input_size

    @property
    def input_name(self) -> str:
        return self._input_name

    @property
    def output_name(self) -> str:
        return self._output_name

    def get_model_info(self) -> dict:
        """获取模型信息"""
        return {
            "loaded": self._is_loaded,
            "version": self._model_version,
            "input_size": self._input_size,
            "input_name": self._input_name,
            "output_name": self._output_name,
            "classes": self.yolo_config.classes,
        }
