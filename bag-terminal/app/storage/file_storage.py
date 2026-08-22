"""本地文件存储管理器。

负责 SD 卡上的图片/视频文件存储：
- 目录管理（按日期 / 巡检 ID 组织）
- 文件命名（时间戳 + 设备 ID + 序号）
- 磁盘空间监控
- 文件清理（LRU + 过期删除）
- 文件校验（SHA-256）

目录结构::

    /mnt/sdcard/bag-terminal/
    ├── photos/
    │   └── 2026-01-15/
    │       └── insp_{inspection_id}/
    │           ├── 20260115_103022_glass01_0001.jpg
    │           └── 20260115_103055_glass01_0002.jpg
    ├── videos/
    │   └── 2026-01-15/
    │       └── insp_{inspection_id}/
    │           └── 20260115_103022_glass01_0001.h264
    ├── alerts/
    │   └── 2026-01-15/
    │       └── alert_{alert_id}.jpg
    ├── logs/
    └── models/
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 默认 SD 卡挂载点
DEFAULT_STORAGE_ROOT = "/mnt/sdcard/bag-terminal"


@dataclass
class DiskInfo:
    """磁盘空间信息。"""

    total: int
    used: int
    free: int
    usage_percent: float

    @property
    def is_full(self) -> bool:
        """使用率是否超过 95%。"""
        return self.usage_percent >= 0.95


@dataclass
class FileStorageConfig:
    """文件存储配置。"""

    storage_root: str = DEFAULT_STORAGE_ROOT
    disk_full_threshold: float = 0.90    # 磁盘使用率阈值
    cleanup_threshold: float = 0.85     # 触发清理的阈值
    max_retention_days: int = 30        # 最大保留天数
    min_free_space_gb: float = 2.0      # 最小保留空间 GB
    photo_dir: str = "photos"
    video_dir: str = "videos"
    alert_dir: str = "alerts"
    log_dir: str = "logs"
    model_dir: str = "models"


class FileStorage:
    """SD 卡文件存储管理器。

    使用示例::

        fs = FileStorage(config)
        path = fs.save_photo(
            data=jpeg_bytes,
            inspection_id="insp-123",
            device_id="glass01",
            ext=".jpg",
        )
        info = fs.get_disk_info()
        if info.is_full:
            fs.cleanup_old_files(days=7)
    """

    def __init__(self, config: FileStorageConfig | None = None) -> None:
        self._config = config or FileStorageConfig()
        self._counter: dict[str, int] = {}  # 设备级序号计数器

    # ── 初始化 ──────────────────────────────────────────────────

    def init_dirs(self) -> None:
        """创建所有必要的目录。"""
        root = Path(self._config.storage_root)
        for subdir in (
            self._config.photo_dir,
            self._config.video_dir,
            self._config.alert_dir,
            self._config.log_dir,
            self._config.model_dir,
        ):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        logger.info("storage_dirs_initialized", root=self._config.storage_root)

    # ── 保存文件 ──────────────────────────────────────────────────

    def save_photo(
        self,
        data: bytes,
        inspection_id: str,
        device_id: str,
        ext: str = ".jpg",
    ) -> str:
        """保存照片文件。

        :param data: JPEG/PNG 二进制数据
        :param inspection_id: 巡检 ID
        :param device_id: 眼镜设备 ID
        :param ext: 文件扩展名
        :return: 保存的绝对路径
        """
        path = self._build_path(
            subdir=self._config.photo_dir,
            inspection_id=inspection_id,
            device_id=device_id,
            ext=ext,
        )
        return self._write_file(path, data)

    def save_video(
        self,
        data: bytes,
        inspection_id: str,
        device_id: str,
        ext: str = ".h264",
    ) -> str:
        """保存视频文件。"""
        path = self._build_path(
            subdir=self._config.video_dir,
            inspection_id=inspection_id,
            device_id=device_id,
            ext=ext,
        )
        return self._write_file(path, data)

    def save_alert_screenshot(
        self,
        data: bytes,
        alert_id: str,
        ext: str = ".jpg",
    ) -> str:
        """保存告警截图。"""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dir_path = Path(self._config.storage_root) / self._config.alert_dir / date_str
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"alert_{alert_id}{ext}"
        path = dir_path / filename
        return self._write_file(str(path), data)

    def save_stream_chunk(
        self,
        data: bytes,
        inspection_id: str,
        device_id: str,
        chunk_index: int,
        ext: str = ".bin",
    ) -> str:
        """保存流式数据块。"""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dir_path = (
            Path(self._config.storage_root)
            / self._config.video_dir
            / date_str
            / f"insp_{inspection_id}"
        )
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"chunk_{chunk_index:06d}_{device_id}{ext}"
        path = dir_path / filename
        return self._write_file(str(path), data)

    def _build_path(
        self,
        subdir: str,
        inspection_id: str,
        device_id: str,
        ext: str,
    ) -> str:
        """构建文件路径：{root}/{subdir}/{date}/insp_{id}/{timestamp}_{device}_{seq}{ext}"""
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%Y%m%d_%H%M%S")
        seq = self._next_seq(device_id)
        dir_path = (
            Path(self._config.storage_root)
            / subdir
            / date_str
            / f"insp_{inspection_id}"
        )
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"{time_str}_{device_id}_{seq:04d}{ext}"
        return str(dir_path / filename)

    def _next_seq(self, device_id: str) -> int:
        """获取设备级递增序号。"""
        self._counter[device_id] = self._counter.get(device_id, 0) + 1
        return self._counter[device_id]

    def _write_file(self, path: str, data: bytes) -> str:
        """写入文件（原子写入：先写临时文件再 rename）。"""
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, path)
            logger.debug("file_saved", path=path, size=len(data))
            return path
        except Exception as exc:  # noqa: BLE001
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            logger.error("file_save_error", path=path, error=str(exc))
            raise

    # ── 文件读取 ──────────────────────────────────────────────────

    def read_file(self, path: str) -> bytes:
        """读取文件内容。"""
        with open(path, "rb") as f:
            return f.read()

    def file_exists(self, path: str) -> bool:
        """检查文件是否存在。"""
        return os.path.exists(path)

    def get_file_size(self, path: str) -> int:
        """获取文件大小。"""
        return os.path.getsize(path)

    def delete_file(self, path: str) -> bool:
        """删除文件。"""
        try:
            os.remove(path)
            logger.debug("file_deleted", path=path)
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("file_delete_error", path=path, error=str(exc))
            return False

    def calculate_sha256(self, path: str) -> str:
        """计算文件 SHA-256 校验值。"""
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    # ── 磁盘监控 ──────────────────────────────────────────────────

    def get_disk_info(self) -> DiskInfo:
        """获取存储目录所在磁盘的空间信息。"""
        stat = os.statvfs(self._config.storage_root)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        usage_percent = used / total if total > 0 else 0.0
        return DiskInfo(
            total=total,
            used=used,
            free=free,
            usage_percent=usage_percent,
        )

    def is_disk_full(self) -> bool:
        """检查磁盘是否已满。"""
        info = self.get_disk_info()
        return (
            info.usage_percent >= self._config.disk_full_threshold
            or info.free < self._config.min_free_space_gb * 1024**3
        )

    def check_and_warn(self) -> bool:
        """检查磁盘空间并记录警告。"""
        info = self.get_disk_info()
        if info.is_full or self.is_disk_full():
            logger.error(
                "disk_full",
                usage_percent=f"{info.usage_percent:.1%}",
                free_gb=f"{info.free / 1024**3:.2f}",
            )
            return True
        if info.usage_percent >= self._config.cleanup_threshold:
            logger.warning(
                "disk_space_low",
                usage_percent=f"{info.usage_percent:.1%}",
                free_gb=f"{info.free / 1024**3:.2f}",
            )
        return False

    # ── 文件清理 ──────────────────────────────────────────────────

    def cleanup_old_files(self, days: int | None = None) -> int:
        """清理超过指定天数的文件。

        :param days: 保留天数（默认使用配置 max_retention_days）
        :return: 删除的文件数
        """
        days = days or self._config.max_retention_days
        cutoff_time = time.time() - (days * 86400)
        deleted_count = 0

        root = Path(self._config.storage_root)
        for subdir in (self._config.photo_dir, self._config.video_dir, self._config.alert_dir):
            base = root / subdir
            if not base.exists():
                continue
            for file_path in base.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    if file_path.stat().st_mtime < cutoff_time:
                        file_path.unlink()
                        deleted_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cleanup_file_error", path=str(file_path), error=str(exc))

        logger.info("cleanup_old_files", deleted=deleted_count, days=days)
        return deleted_count

    def cleanup_by_lru(self, target_free_gb: float = 5.0) -> int:
        """按 LRU（最近最少使用）策略清理文件，直到释放足够空间。

        :param target_free_gb: 目标释放空间 GB
        :return: 删除的文件数
        """
        target_bytes = int(target_free_gb * 1024**3)
        freed_bytes = 0
        deleted_count = 0

        root = Path(self._config.storage_root)
        all_files: list[tuple[Path, float, int]] = []

        for subdir in (self._config.photo_dir, self._config.video_dir, self._config.alert_dir):
            base = root / subdir
            if not base.exists():
                continue
            for file_path in base.rglob("*"):
                if not file_path.is_file():
                    continue
                stat = file_path.stat()
                all_files.append((file_path, stat.st_mtime, stat.st_size))

        # 按 mtime 升序排序（最旧的先删）
        all_files.sort(key=lambda x: x[1])

        for file_path, _mtime, size in all_files:
            if freed_bytes >= target_bytes:
                break
            try:
                file_path.unlink()
                freed_bytes += size
                deleted_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("lru_cleanup_error", path=str(file_path), error=str(exc))

        logger.info(
            "lru_cleanup_done",
            deleted=deleted_count,
            freed_gb=f"{freed_bytes / 1024**3:.2f}",
        )
        return deleted_count

    def cleanup_orphaned_dirs(self) -> int:
        """清理空目录。"""
        removed = 0
        root = Path(self._config.storage_root)
        for subdir in (self._config.photo_dir, self._config.video_dir, self._config.alert_dir):
            base = root / subdir
            if not base.exists():
                continue
            for dir_path in sorted(base.rglob("*"), reverse=True):
                if dir_path.is_dir() and not any(dir_path.iterdir()):
                    dir_path.rmdir()
                    removed += 1
        logger.info("orphaned_dirs_cleaned", removed=removed)
        return removed

    # ── 列举文件 ──────────────────────────────────────────────────

    def list_inspection_files(
        self,
        inspection_id: str,
        subdir: str | None = None,
    ) -> list[str]:
        """列举某巡检 ID 下的所有文件。"""
        results: list[str] = []
        root = Path(self._config.storage_root)
        subdirs = [subdir] if subdir else [
            self._config.photo_dir,
            self._config.video_dir,
            self._config.alert_dir,
        ]
        for sd in subdirs:
            pattern = f"insp_{inspection_id}"
            base = root / sd
            if not base.exists():
                continue
            for dir_path in base.rglob(pattern):
                if dir_path.is_dir():
                    for file_path in dir_path.iterdir():
                        if file_path.is_file():
                            results.append(str(file_path))
        return sorted(results)

    def get_storage_stats(self) -> dict[str, Any]:
        """获取存储统计。"""
        info = self.get_disk_info()
        root = Path(self._config.storage_root)
        file_count = 0
        total_size = 0
        for subdir in (self._config.photo_dir, self._config.video_dir, self._config.alert_dir):
            base = root / subdir
            if not base.exists():
                continue
            for file_path in base.rglob("*"):
                if file_path.is_file():
                    stat = file_path.stat()
                    file_count += 1
                    total_size += stat.st_size
        return {
            "disk_total_bytes": info.total,
            "disk_used_bytes": info.used,
            "disk_free_bytes": info.free,
            "disk_usage_percent": round(info.usage_percent, 4),
            "disk_is_full": info.is_full,
            "file_count": file_count,
            "files_total_bytes": total_size,
        }
