"""
模型版本管理器

功能:
    - 加载/卸载 YOLO + 嵌入 + LLM 模型
    - 热更新 (定期检查模型注册中心, 下载新版本)
    - 版本追踪 (当前版本 + 可用版本列表)
    - 回滚 (切换到上一个版本)

模型文件结构:
    models/
      yolo/
        yolov8n_railway.onnx          — 当前激活版本
        yolov8n_railway_v1.1.0.onnx   — 历史版本
        yolov8n_railway_v1.0.0.onnx   — 历史版本
      embedding/
        bge-small-zh.onnx
      llm/
        phi-3-mini-q4.gguf
      manifest.json                    — 模型清单 (版本/SHA-256/路径)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.ai.image_processor import ImageProcessor
from app.ai.yolo_inference import YoloInference
from app.ai.detection_postprocessor import DetectionPostprocessor, PostprocessResult
from app.config import Settings

logger = structlog.get_logger(__name__)


@dataclass
class ModelVersion:
    """模型版本信息"""
    name: str                     # 模型名称
    version: str                  # 版本号
    file_path: str                # 文件路径
    sha256: str = ""              # SHA-256 校验值
    file_size: int = 0            # 文件大小 (字节)
    loaded_at: str = ""           # 加载时间
    is_active: bool = False        # 是否当前激活

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "file_size": self.file_size,
            "loaded_at": self.loaded_at,
            "is_active": self.is_active,
        }


@dataclass
class ModelManifest:
    """模型清单"""
    yolo_versions: list[ModelVersion] = field(default_factory=list)
    active_yolo_version: str = ""
    last_updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "yolo_versions": [v.to_dict() for v in self.yolo_versions],
            "active_yolo_version": self.active_yolo_version,
            "last_updated": self.last_updated,
        }


class ModelManager:
    """
    模型版本管理器

    管理 YOLO 推理模型的生命周期, 支持热更新和回滚。
    嵌入模型和 LLM 由 RAG 引擎管理, 此处仅管理 YOLO。
    """

    MANIFEST_FILENAME = "manifest.json"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.yolo_config = settings.models.yolo
        self.model_dir = Path(settings.models.model_dir)

        # 推理组件
        self._yolo: YoloInference | None = None
        self._image_processor: ImageProcessor | None = None
        self._postprocessor: DetectionPostprocessor | None = None

        # 版本管理
        self._manifest: ModelManifest = ModelManifest()
        self._current_version: str = ""
        self._previous_version: str = ""
        self._available_versions: list[str] = []
        self._is_loaded: bool = False

        # 热更新任务
        self._hot_update_task: asyncio.Task | None = None

    # -------------------------------------------------------------------
    # 初始化
    # -------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化模型管理器, 加载模型"""
        # 创建模型目录
        self.model_dir.mkdir(parents=True, exist_ok=True)
        (self.model_dir / "yolo").mkdir(exist_ok=True)

        # 加载清单
        self._load_manifest()

        # 创建推理组件
        self._image_processor = ImageProcessor(
            input_size=self.yolo_config.input_size,
        )
        self._postprocessor = DetectionPostprocessor(self.settings)

        # 加载 YOLO 模型
        await self._load_yolo_model()

        # 启动热更新检查
        if self.yolo_config.hot_update_interval > 0:
            self._hot_update_task = asyncio.create_task(self._hot_update_loop())

        logger.info(
            "model_manager.initialized",
            yolo_version=self._current_version,
            available_versions=self._available_versions,
        )

    async def _load_yolo_model(self) -> None:
        """加载 YOLO 模型"""
        model_path = self.yolo_config.model_path

        # 如果指定路径不存在, 尝试在 model_dir/yolo/ 下查找
        path = Path(model_path)
        if not path.exists():
            yolo_dir = self.model_dir / "yolo"
            candidates = list(yolo_dir.glob("*.onnx"))
            if candidates:
                path = candidates[0]
                model_path = str(path)
            else:
                logger.warning(
                    "model_manager.no_yolo_model",
                    path=model_path,
                )
                return

        # 推断版本号 (从文件名)
        version = self._infer_version(path.name)

        # 创建 YOLO 推理器并加载
        self._yolo = YoloInference(self.settings)
        await self._yolo.load_model(str(path), version)

        self._previous_version = self._current_version
        self._current_version = version
        self._is_loaded = True

        # 更新清单
        self._update_manifest(path, version)

        logger.info(
            "model_manager.yolo_loaded",
            path=str(path),
            version=version,
            input_size=self._yolo.input_size,
        )

    def _infer_version(self, filename: str) -> str:
        """从文件名推断版本号"""
        # yolov8n_railway_v1.2.0.onnx → 1.2.0
        # yolov8n_railway.onnx → 1.0.0 (默认)
        parts = filename.replace(".onnx", "").split("_")
        for part in reversed(parts):
            if part.startswith("v") and part[1:].count(".") >= 1:
                return part[1:]
            if part.count(".") == 2 and part.replace(".", "").isdigit():
                return part
        return "1.0.0"

    # -------------------------------------------------------------------
    # 推理接口
    # -------------------------------------------------------------------

    async def detect(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """
        完整检测流程: 预处理 → 推理 → 后处理

        Args:
            image_bytes: JPEG/PNG 图片字节

        Returns:
            检测结果列表 [{class_id, class_name, confidence, x, y, w, h}]
        """
        if not self._is_loaded or not self._yolo:
            raise RuntimeError("模型未加载")

        # 1. 预处理
        preprocess_result = await asyncio.get_event_loop().run_in_executor(
            None,
            self._image_processor.preprocess,
            image_bytes,
        )

        # 2. 推理
        inference_result = await self._yolo.infer(preprocess_result)

        # 3. 后处理
        postprocess_result = self._postprocessor.postprocess(
            inference_result,
            preprocess_result,
        )

        logger.debug(
            "model_manager.detect",
            detections=postprocess_result.detection_count,
            pre_ms=round(preprocess_result.elapsed_ms, 2),
            infer_ms=round(inference_result.elapsed_ms, 2),
            post_ms=round(postprocess_result.elapsed_ms, 2),
        )

        return self._postprocessor.serialize_to_dict(postprocess_result)["detections"]

    async def detect_full(self, image_bytes: bytes) -> PostprocessResult:
        """完整检测流程, 返回 PostprocessResult (含性能数据)"""
        if not self._is_loaded or not self._yolo:
            raise RuntimeError("模型未加载")

        preprocess_result = await asyncio.get_event_loop().run_in_executor(
            None,
            self._image_processor.preprocess,
            image_bytes,
        )
        inference_result = await self._yolo.infer(preprocess_result)
        postprocess_result = self._postprocessor.postprocess(
            inference_result, preprocess_result
        )
        return postprocess_result

    # -------------------------------------------------------------------
    # 热更新
    # -------------------------------------------------------------------

    async def _hot_update_loop(self) -> None:
        """定期检查模型更新 (v2.0: hot_update_interval=0 时禁用)"""
        if self.yolo_config.hot_update_interval <= 0:
            logger.info("model_manager.hot_update_disabled", reason="interval_is_zero")
            return

        while True:
            try:
                await asyncio.sleep(self.yolo_config.hot_update_interval)
                await self.check_and_update()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("model_manager.hot_update_error", error=str(exc))
                await asyncio.sleep(60)

    async def check_and_update(self) -> bool:
        """
        检查并下载新版本模型 (v2.0: 已禁用云端更新)

        Returns: True 如果更新了模型
        """
        # 云端模型热更新已在 v2.0 等保架构中禁用
        # 如需更新模型, 请使用 import_from_usb()
        logger.debug("model_manager.cloud_update_disabled")
        return False

    async def import_from_usb(self) -> bool:
        """
        从 USB 导入新模型 (PC 推送到 models 目录)

        流程:
            1. 扫描 models/yolo/ 目录中的新 .onnx 文件
            2. 验证文件完整性 (SHA-256 与 manifest.json 比对)
            3. 备份当前版本
            4. 加载新版本
            5. 更新版本号

        Returns: True 如果成功更新模型
        """
        import hashlib

        yolo_dir = self.model_dir / "yolo"
        if not yolo_dir.exists():
            logger.warning("model_manager.yolo_dir_not_found", path=str(yolo_dir))
            return False

        # 查找新模型文件 (排除当前使用的)
        current_path = Path(self.yolo_config.model_path).name

        for onnx_file in sorted(yolo_dir.glob("yolov8n_railway_v*.onnx")):
            if onnx_file.name == current_path:
                continue

            # 提取版本号
            version = onnx_file.stem.replace("yolov8n_railway_v", "")

            # 验证 SHA-256 (如果 manifest.json 存在)
            manifest_path = yolo_dir / "manifest.json"
            if manifest_path.exists():
                import json
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                expected_sha = None
                for entry in manifest.get("models", []):
                    if entry.get("version") == version:
                        expected_sha = entry.get("sha256")
                        break

                if expected_sha:
                    actual_sha = hashlib.sha256(onnx_file.read_bytes()).hexdigest()
                    if actual_sha != expected_sha:
                        logger.error(
                            "model_manager.import_sha_mismatch",
                            file=onnx_file.name,
                            expected=expected_sha,
                            actual=actual_sha,
                        )
                        continue

            # 切换到新版本
            try:
                await self._switch_version(str(onnx_file), version)
                logger.info(
                    "model_manager.usb_import_success",
                    version=version,
                    file=onnx_file.name,
                )
                return True
            except Exception as exc:
                logger.error(
                    "model_manager.usb_import_failed",
                    file=onnx_file.name,
                    error=str(exc),
                )
                continue

        logger.debug("model_manager.no_new_model_found")
        return False

    async def _switch_version(self, model_path: str, version: str) -> None:
        """切换到指定版本"""
        # 保存当前版本信息
        self._previous_version = self._current_version

        # 卸载当前模型
        if self._yolo:
            await self._yolo.unload_model()

        # 加载新版本
        await self._yolo.load_model(model_path, version)
        self._current_version = version

        # 更新清单
        self._update_manifest(Path(model_path), version)

        # 更新配置
        self.yolo_config.model_path = model_path

    async def rollback(self) -> bool:
        """
        回滚到上一个版本

        Returns: True 回滚成功
        """
        if not self._previous_version:
            logger.warning("model_manager.no_previous_version")
            return False

        # 查找上一个版本的模型文件
        prev_path = self.model_dir / "yolo" / f"yolov8n_railway_v{self._previous_version}.onnx"
        if not prev_path.exists():
            logger.warning(
                "model_manager.rollback_file_missing",
                version=self._previous_version,
                path=str(prev_path),
            )
            return False

        old_current = self._current_version
        await self._switch_version(str(prev_path), self._previous_version)
        self._previous_version = old_current

        logger.info(
            "model_manager.rolled_back",
            from_version=old_current,
            to_version=self._current_version,
        )
        return True

    # -------------------------------------------------------------------
    # 清单管理
    # -------------------------------------------------------------------

    def _load_manifest(self) -> None:
        """加载模型清单"""
        manifest_path = self.model_dir / self.MANIFEST_FILENAME
        if not manifest_path.exists():
            return

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._manifest.active_yolo_version = data.get("active_yolo_version", "")
            self._manifest.last_updated = data.get("last_updated", "")
            self._manifest.yolo_versions = [
                ModelVersion(**v) for v in data.get("yolo_versions", [])
            ]
            self._available_versions = [v.version for v in self._manifest.yolo_versions]
        except Exception as exc:
            logger.warning("model_manager.manifest_load_error", error=str(exc))

    def _save_manifest(self) -> None:
        """保存模型清单"""
        manifest_path = self.model_dir / self.MANIFEST_FILENAME
        try:
            manifest_path.write_text(
                json.dumps(self._manifest.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("model_manager.manifest_save_error", error=str(exc))

    def _update_manifest(self, model_path: Path, version: str) -> None:
        """更新清单中的版本信息"""
        import hashlib

        try:
            file_stat = model_path.stat()
            sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        except Exception:
            sha256 = ""
            file_stat = None

        # 标记所有版本为非活跃
        for v in self._manifest.yolo_versions:
            v.is_active = False

        # 查找或创建当前版本记录
        existing = None
        for v in self._manifest.yolo_versions:
            if v.version == version:
                existing = v
                break

        if existing:
            existing.is_active = True
            existing.loaded_at = datetime.now(timezone.utc).isoformat()
        else:
            new_version = ModelVersion(
                name="yolov8n_railway",
                version=version,
                file_path=str(model_path),
                sha256=sha256,
                file_size=file_stat.st_size if file_stat else 0,
                loaded_at=datetime.now(timezone.utc).isoformat(),
                is_active=True,
            )
            self._manifest.yolo_versions.append(new_version)

        self._manifest.active_yolo_version = version
        self._manifest.last_updated = datetime.now(timezone.utc).isoformat()
        self._available_versions = [v.version for v in self._manifest.yolo_versions]

        self._save_manifest()

    # -------------------------------------------------------------------
    # 状态查询
    # -------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    @property
    def current_version(self) -> str:
        return self._current_version

    @property
    def previous_version(self) -> str:
        return self._previous_version

    @property
    def available_versions(self) -> list[str]:
        return self._available_versions

    def get_model_info(self) -> dict[str, Any]:
        """获取模型信息"""
        if self._yolo:
            info = self._yolo.get_model_info()
            info["current_version"] = self._current_version
            info["previous_version"] = self._previous_version
            info["available_versions"] = self._available_versions
            return info
        return {"loaded": False}

    # -------------------------------------------------------------------
    # 关闭
    # -------------------------------------------------------------------

    async def shutdown(self) -> None:
        """关闭模型管理器, 释放资源"""
        if self._hot_update_task:
            self._hot_update_task.cancel()
            try:
                await self._hot_update_task
            except asyncio.CancelledError:
                pass

        if self._yolo:
            await self._yolo.unload_model()

        self._is_loaded = False
        logger.info("model_manager.shutdown")
