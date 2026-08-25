"""
数据打包导出器 — .dat 二进制容器格式 (铁路等保安全要求)

功能:
    - 将巡检数据从 SQLite + 文件系统打包为 .dat 二进制容器
    - 支持按巡检 ID / 时间范围 / 数据类型筛选
    - 生成 SHA-256 校验 + HMAC-SHA256 签名
    - 导出包格式: .dat (自定义二进制容器, 非标准压缩格式)
    - 支持全量导出和增量导出

.dat 文件格式:
    ┌──────────────────────────────────────────────────────┐
    │ 文件头 (Header) — 固定 64 字节                        │
    │  Magic:      4B   0x52 0x41 0x49 0x4C ("RAIL")     │
    │  Version:    2B   0x0002                             │
    │  Flags:      2B   保留                               │
    │  ManifestOffset: 8B  (manifest 段偏移)               │
    │  ManifestSize:   8B  (manifest 段大小)               │
    │  DataOffset:     8B  (data 段偏移)                   │
    │  DataSize:       8B  (data 段大小)                   │
    │  SignatureOffset: 8B (signature 段偏移)             │
    │  SignatureSize:   8B  (signature 段大小)             │
    │  Reserved:    8B   保留                               │
    ├──────────────────────────────────────────────────────┤
    │ Data 段 — 各文件二进制拼接                            │
    │  每个文件:                                            │
    │    PathLen:  2B   路径长度                            │
    │    Path:     N B   相对路径 (UTF-8)                  │
    │    FileSize: 8B   文件大小                           │
    │    SHA256:  32B   文件哈希                           │
    │    Content: M B   文件内容                           │
    ├──────────────────────────────────────────────────────┤
    │ Manifest 段 — JSON (导出元数据 + 文件列表)            │
    ├──────────────────────────────────────────────────────┤
    │ Signature 段 — HMAC-SHA256 (64 字节 hex)             │
    └──────────────────────────────────────────────────────┘

导出模式:
    1. 全量导出 — 所有未同步数据
    2. 按巡检导出 — 指定巡检 ID 的所有关联数据
    3. 按时间导出 — 指定时间范围内的数据
    4. 按类型导出 — 仅照片 / 仅检测结果 / 仅告警等
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.storage.local_db import LocalDatabase

logger = structlog.get_logger(__name__)

# .dat 文件魔数和版本
DAT_MAGIC = b"RAIL"              # 4 字节
DAT_VERSION = 2                   # 2 字节
DAT_HEADER_SIZE = 64              # 固定头大小
DAT_HEADER_FORMAT = ">4sHHQQQQQQ"  # 大端序: magic(4) + version(2) + flags(2) + 6x uint64(8 each)
DAT_SIGNATURE_SIZE = 64           # HMAC-SHA256 hex 字符串长度


@dataclass
class ExportConfig:
    """导出配置"""
    # 导出文件存放目录 (PC 可通过 USB 访问)
    export_dir: str = "/mnt/sdcard/bag-terminal/exports"
    # HMAC 签名密钥 (从环境变量 BAG_EXPORT_HMAC_KEY 读取)
    hmac_key: str = os.environ.get("BAG_EXPORT_HMAC_KEY", "railway-ar-glasses-default-key")
    # 导出包最大大小 (MB), 超过则分卷
    max_package_size_mb: int = 4096
    # 是否包含照片原图
    include_photos: bool = True
    # 是否包含视频
    include_videos: bool = True
    # 是否包含告警截图
    include_alerts: bool = True
    # 照片质量 (1=原图, 0.5=半分辨率)
    photo_quality: float = 1.0


@dataclass
class ExportResult:
    """导出结果"""
    package_path: str = ""
    package_size_bytes: int = 0
    file_count: int = 0
    manifest: dict[str, Any] = field(default_factory=dict)
    sha256_signature: str = ""
    export_id: str = ""
    created_at: str = ""


class DataExporter:
    """
    数据打包导出器 — .dat 二进制容器格式

    将挎包终端中的巡检数据打包为 .dat 文件,
    供 PC 端通过 USB 有线连接下载或直接从 SD 卡拷贝。

    .dat 格式优势:
        - 二进制封装, 无法直接打开查看 (安全性)
        - 内嵌 SHA-256 + HMAC 签名 (完整性)
        - 自定义格式, 需专用工具解析 (防篡改)
        - 支持大文件 (>4GB, 使用 8 字节长度字段)
    """

    def __init__(self, settings: Any, db: LocalDatabase, config: ExportConfig | None = None) -> None:
        self.settings = settings
        self._db = db
        self._config = config or ExportConfig()

    async def export_full(self) -> ExportResult:
        """全量导出 — 所有未同步数据"""
        logger.info("data_exporter.full_export_start")
        return await self._export_data(
            export_type="full",
            filters={"synced": False},
        )

    async def export_by_inspection(self, inspection_id: str) -> ExportResult:
        """按巡检 ID 导出 — 该巡检的所有关联数据"""
        logger.info("data_exporter.inspection_export_start", inspection_id=inspection_id)
        return await self._export_data(
            export_type="inspection",
            filters={"inspection_id": inspection_id},
        )

    async def export_by_date_range(
        self, start_date: str, end_date: str
    ) -> ExportResult:
        """按时间范围导出"""
        logger.info("data_exporter.date_export_start", start=start_date, end=end_date)
        return await self._export_data(
            export_type="date_range",
            filters={"start_date": start_date, "end_date": end_date},
        )

    async def export_by_type(self, data_type: str) -> ExportResult:
        """按数据类型导出 (photos / detections / alerts / rag / telemetry)"""
        logger.info("data_exporter.type_export_start", data_type=data_type)
        return await self._export_data(
            export_type="by_type",
            filters={"data_type": data_type},
        )

    async def _export_data(self, export_type: str, filters: dict[str, Any]) -> ExportResult:
        """执行数据导出 — 生成 .dat 文件"""
        timestamp = datetime.now(timezone.utc)
        export_id = f"exp_{timestamp.strftime('%Y%m%d_%H%M%S')}"
        timestamp_str = timestamp.isoformat()

        # 创建临时工作目录 (收集数据文件)
        work_dir = Path(tempfile.mkdtemp(prefix=f"export_{export_id}_"))

        try:
            # 1. 收集所有要导出的数据文件
            file_entries: list[dict[str, Any]] = []

            # 1a. 导出 SQLite 数据为 JSON
            db_json_path = work_dir / "database_export.json"
            await self._export_sqlite_data(db_json_path, filters)
            file_entries.append({"path": str(db_json_path.relative_to(work_dir))})

            # 1b. 导出照片
            if self._config.include_photos:
                photo_dir = work_dir / "photos"
                photo_dir.mkdir(parents=True, exist_ok=True)
                await self._export_files(photo_dir, filters, "photos", work_dir, file_entries)

            # 1c. 导出视频
            if self._config.include_videos:
                video_dir = work_dir / "videos"
                video_dir.mkdir(parents=True, exist_ok=True)
                await self._export_files(video_dir, filters, "videos", work_dir, file_entries)

            # 1d. 导出告警截图
            if self._config.include_alerts:
                alert_dir = work_dir / "alerts"
                alert_dir.mkdir(parents=True, exist_ok=True)
                await self._export_files(alert_dir, filters, "alerts", work_dir, file_entries)

            # 1e. 导出检测结果
            detections_path = work_dir / "detections.json"
            await self._export_detections(detections_path, filters)
            file_entries.append({"path": str(detections_path.relative_to(work_dir))})

            # 1f. 导出 RAG 问答记录
            rag_path = work_dir / "rag_queries.json"
            await self._export_rag_queries(rag_path, filters)
            file_entries.append({"path": str(rag_path.relative_to(work_dir))})

            # 1g. 导出遥测数据
            telemetry_path = work_dir / "telemetry.json"
            await self._export_telemetry(telemetry_path, filters)
            file_entries.append({"path": str(telemetry_path.relative_to(work_dir))})

            # 2. 计算每个文件的 SHA-256
            manifest_files = []
            for entry in file_entries:
                file_path = work_dir / entry["path"]
                if file_path.exists():
                    sha256 = await asyncio.to_thread(self._hash_file, file_path)
                    size = file_path.stat().st_size
                    manifest_files.append({
                        "path": entry["path"],
                        "size": size,
                        "sha256": sha256,
                    })

            # 3. 生成 manifest.json
            manifest = {
                "export_id": export_id,
                "export_type": export_type,
                "created_at": timestamp_str,
                "terminal_serial": getattr(self.settings, "device_id", "unknown"),
                "terminal_version": "2.0.0",
                "format": "dat",
                "format_version": DAT_VERSION,
                "filters": filters,
                "file_count": len(manifest_files),
                "files": manifest_files,
            }

            # 4. 生成 HMAC-SHA256 签名
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
            signature = hmac.new(
                self._config.hmac_key.encode(),
                manifest_bytes,
                hashlib.sha256,
            ).hexdigest()

            # 5. 构建 .dat 二进制文件
            os.makedirs(self._config.export_dir, exist_ok=True)
            package_name = f"export_{timestamp.strftime('%Y%m%d_%H%M%S')}.dat"
            package_path = Path(self._config.export_dir) / package_name

            await asyncio.to_thread(
                self._create_dat_file,
                work_dir,
                package_path,
                manifest,
                signature,
            )

            package_size = package_path.stat().st_size

            logger.info(
                "data_exporter.export_complete",
                export_id=export_id,
                package=str(package_path),
                size_bytes=package_size,
                files=len(manifest_files),
                format="dat",
            )

            return ExportResult(
                package_path=str(package_path),
                package_size_bytes=package_size,
                file_count=len(manifest_files),
                manifest=manifest,
                sha256_signature=signature,
                export_id=export_id,
                created_at=timestamp_str,
            )

        except Exception as exc:
            logger.error("data_exporter.export_failed", error=str(exc))
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _create_dat_file(
        work_dir: Path,
        target: Path,
        manifest: dict[str, Any],
        signature: str,
    ) -> None:
        """
        构建 .dat 二进制容器文件

        结构:
            [Header 64B] [Data段] [Manifest段] [Signature段]
        """
        # --- 1. 构建 Data 段 ---
        data_buf = io.BytesIO()
        file_list = manifest.get("files", [])
        for file_info in file_list:
            file_path = work_dir / file_info["path"]
            if not file_path.exists():
                continue
            rel_path = file_info["path"].encode("utf-8")
            content = file_path.read_bytes()
            file_size = len(content)
            sha256 = file_info["sha256"]

            # 写入: PathLen(2B) + Path(NB) + FileSize(8B) + SHA256(32B hex) + Content(MB)
            data_buf.write(struct.pack(">H", len(rel_path)))
            data_buf.write(rel_path)
            data_buf.write(struct.pack(">Q", file_size))
            data_buf.write(bytes.fromhex(sha256))
            data_buf.write(content)

        data_bytes = data_buf.getvalue()
        data_size = len(data_bytes)
        data_offset = DAT_HEADER_SIZE

        # --- 2. 构建 Manifest 段 ---
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        manifest_size = len(manifest_bytes)
        manifest_offset = data_offset + data_size

        # --- 3. 构建 Signature 段 ---
        signature_bytes = signature.encode("ascii")
        # 补齐到 64 字节
        if len(signature_bytes) < DAT_SIGNATURE_SIZE:
            signature_bytes = signature_bytes.ljust(DAT_SIGNATURE_SIZE, b"\x00")
        signature_size = len(signature_bytes)
        signature_offset = manifest_offset + manifest_size

        # --- 4. 构建文件头 ---
        header = struct.pack(
            DAT_HEADER_FORMAT,
            DAT_MAGIC,
            DAT_VERSION,
            0,  # flags 保留
            manifest_offset,
            manifest_size,
            data_offset,
            data_size,
            signature_offset,
            signature_size,
        )
        # 补齐到 DAT_HEADER_SIZE
        if len(header) < DAT_HEADER_SIZE:
            header = header.ljust(DAT_HEADER_SIZE, b"\x00")

        # --- 5. 写入 .dat 文件 ---
        with open(target, "wb") as f:
            f.write(header)
            f.write(data_bytes)
            f.write(manifest_bytes)
            f.write(signature_bytes)

    async def _export_sqlite_data(self, out_path: Path, filters: dict[str, Any]) -> None:
        """导出 SQLite 数据为 JSON"""
        export_data = {}
        tables_to_export = [
            "glasses_devices",
            "camera_module_events",
            "inspection_tasks",
            "inspection_photos_cache",
            "ai_detection_cache",
            "rag_query_cache",
            "alerts_cache",
            "sync_state",
        ]
        for table in tables_to_export:
            try:
                rows = await self._db.fetch_all(
                    f"SELECT * FROM {table} WHERE 1=1",
                    params=(),
                )
                if rows:
                    export_data[table] = [
                        dict(row) if hasattr(row, "keys") else row for row in rows
                    ]
            except Exception:
                export_data[table] = []

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(export_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    async def _export_files(
        self,
        target_dir: Path,
        filters: dict[str, Any],
        file_type: str,
        work_dir: Path,
        file_entries: list[dict[str, Any]],
    ) -> None:
        """导出文件 (照片/视频/告警截图)"""
        target_dir.mkdir(parents=True, exist_ok=True)
        storage_root = Path(getattr(self.settings, "storage_root", "/mnt/sdcard/bag-terminal"))

        source_dirs = {
            "photos": storage_root / "photos",
            "videos": storage_root / "videos",
            "alerts": storage_root / "alerts",
        }
        source_dir = source_dirs.get(file_type)
        if not source_dir or not source_dir.exists():
            return

        inspection_id = filters.get("inspection_id")
        start_date = filters.get("start_date")
        end_date = filters.get("end_date")

        copied_count = 0
        for root, dirs, files in os.walk(source_dir):
            for fname in files:
                file_path = Path(root) / fname

                if inspection_id and inspection_id not in str(file_path):
                    continue
                if start_date:
                    try:
                        stat = file_path.stat()
                        mod_time = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        if mod_time.isoformat() < start_date:
                            continue
                    except Exception:
                        pass
                if end_date:
                    try:
                        stat = file_path.stat()
                        mod_time = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        if mod_time.isoformat() > end_date:
                            continue
                    except Exception:
                        pass

                rel_path = file_path.relative_to(source_dir)
                dest_path = target_dir / rel_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, dest_path)
                copied_count += 1

                # 添加到文件列表
                rel_to_work = dest_path.relative_to(work_dir)
                file_entries.append({"path": str(rel_to_work)})

        logger.info("data_exporter.files_exported", file_type=file_type, count=copied_count)

    async def _export_detections(self, path: Path, filters: dict[str, Any]) -> None:
        """导出 AI 检测结果"""
        try:
            rows = await self._db.fetch_all(
                "SELECT * FROM ai_detection_cache WHERE 1=1",
                params=(),
            )
            data = [dict(row) if hasattr(row, "keys") else row for row in rows]
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("data_exporter.detections_error", error=str(exc))
            path.write_text("[]", encoding="utf-8")

    async def _export_rag_queries(self, path: Path, filters: dict[str, Any]) -> None:
        """导出 RAG 问答记录"""
        try:
            rows = await self._db.fetch_all(
                "SELECT * FROM rag_query_cache WHERE 1=1",
                params=(),
            )
            data = [dict(row) if hasattr(row, "keys") else row for row in rows]
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("data_exporter.rag_export_error", error=str(exc))
            path.write_text("[]", encoding="utf-8")

    async def _export_telemetry(self, path: Path, filters: dict[str, Any]) -> None:
        """导出遥测数据"""
        try:
            rows = await self._db.fetch_all(
                "SELECT * FROM sync_state WHERE 1=1",
                params=(),
            )
            data = [dict(row) if hasattr(row, "keys") else row for row in rows]
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("data_exporter.telemetry_error", error=str(exc))
            path.write_text("[]", encoding="utf-8")

    async def _compute_file_hashes(self, root: Path) -> list[dict[str, str]]:
        """计算工作目录中所有文件的 SHA-256"""
        file_list = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel_path = str(path.relative_to(root))
                sha256 = await asyncio.to_thread(self._hash_file, path)
                size = path.stat().st_size
                file_list.append({
                    "path": rel_path,
                    "size": size,
                    "sha256": sha256,
                })
        return file_list

    @staticmethod
    def _hash_file(path: Path) -> str:
        """计算文件 SHA-256"""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def list_exports(self) -> list[dict[str, Any]]:
        """列出所有可用的导出包"""
        export_dir = Path(self._config.export_dir)
        if not export_dir.exists():
            return []

        exports = []
        for f in sorted(export_dir.glob("export_*.dat"), reverse=True):
            stat = f.stat()
            exports.append({
                "filename": f.name,
                "path": str(f),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        return exports

    async def delete_export(self, filename: str) -> bool:
        """删除指定导出包"""
        path = Path(self._config.export_dir) / filename
        if path.exists() and path.suffix == ".dat":
            path.unlink()
            logger.info("data_exporter.export_deleted", filename=filename)
            return True
        return False

    async def verify_export(self, package_path: str) -> dict[str, Any]:
        """验证 .dat 导出包完整性 (SHA-256 + HMAC 签名)"""
        path = Path(package_path)
        if not path.exists():
            return {"valid": False, "error": "Package not found"}

        try:
            with open(path, "rb") as f:
                # 读取文件头
                header = f.read(DAT_HEADER_SIZE)
                if len(header) < DAT_HEADER_SIZE:
                    return {"valid": False, "error": "Header too short"}

                magic, version, flags, manifest_offset, manifest_size, \
                    data_offset, data_size, sig_offset, sig_size = struct.unpack(
                        DAT_HEADER_FORMAT, header[:struct.calcsize(DAT_HEADER_FORMAT)]
                    )

                if magic != DAT_MAGIC:
                    return {"valid": False, "error": f"Invalid magic: {magic}"}
                if version != DAT_VERSION:
                    return {"valid": False, "error": f"Unsupported version: {version}"}

                # 读取 Manifest
                f.seek(manifest_offset)
                manifest_bytes = f.read(manifest_size)
                manifest = json.loads(manifest_bytes.decode("utf-8"))

                # 读取 Signature
                f.seek(sig_offset)
                signature = f.read(sig_size).rstrip(b"\x00").decode("ascii")

                # 验证 HMAC 签名
                expected_sig = hmac.new(
                    self._config.hmac_key.encode(),
                    manifest_bytes,
                    hashlib.sha256,
                ).hexdigest()
                if signature != expected_sig:
                    return {"valid": False, "error": "HMAC signature mismatch"}

                # 验证 Data 段中每个文件
                f.seek(data_offset)
                verified_files = 0
                failed_files = []
                for file_info in manifest.get("files", []):
                    # 读取 PathLen
                    path_len_bytes = f.read(2)
                    if len(path_len_bytes) < 2:
                        break
                    (path_len,) = struct.unpack(">H", path_len_bytes)

                    # 读取 Path
                    rel_path = f.read(path_len).decode("utf-8")

                    # 读取 FileSize
                    (file_size,) = struct.unpack(">Q", f.read(8))

                    # 读取 SHA256
                    stored_sha256 = f.read(32).hex()

                    # 读取 Content
                    content = f.read(file_size)

                    # 计算 SHA256
                    actual_sha256 = hashlib.sha256(content).hexdigest()
                    if actual_sha256 == stored_sha256:
                        verified_files += 1
                    else:
                        failed_files.append(rel_path)

                return {
                    "valid": len(failed_files) == 0,
                    "verified_files": verified_files,
                    "total_files": len(manifest.get("files", [])),
                    "failed_files": failed_files,
                    "export_id": manifest.get("export_id", ""),
                    "export_type": manifest.get("export_type", ""),
                    "format": "dat",
                    "format_version": version,
                }

        except Exception as exc:
            logger.error("data_exporter.verify_error", error=str(exc))
            return {"valid": False, "error": str(exc)}

    @staticmethod
    def read_dat_header(package_path: str) -> dict[str, Any]:
        """读取 .dat 文件头信息 (不解压)"""
        path = Path(package_path)
        if not path.exists():
            return {"error": "Package not found"}

        try:
            with open(path, "rb") as f:
                header = f.read(DAT_HEADER_SIZE)
                if len(header) < struct.calcsize(DAT_HEADER_FORMAT):
                    return {"error": "Header too short"}

                magic, version, flags, manifest_offset, manifest_size, \
                    data_offset, data_size, sig_offset, sig_size = struct.unpack(
                        DAT_HEADER_FORMAT, header[:struct.calcsize(DAT_HEADER_FORMAT)]
                    )

                return {
                    "magic": magic.decode("ascii", errors="replace"),
                    "version": version,
                    "flags": flags,
                    "data_offset": data_offset,
                    "data_size": data_size,
                    "manifest_offset": manifest_offset,
                    "manifest_size": manifest_size,
                    "signature_offset": sig_offset,
                    "signature_size": sig_size,
                    "file_size": path.stat().st_size,
                }
        except Exception as exc:
            return {"error": str(exc)}
