"""SaaS 后端 REST API 客户端。

基于 httpx 异步客户端，负责挎包终端与 SaaS 后端的所有 HTTP 通信：
- JWT 认证（自动刷新 Token）
- 巡检任务同步（拉取 / 上传）
- 照片上传（分块上传到 MinIO）
- RAG 远程回退（本地 RAG 失败时调用 SaaS Dify）
- 告警上报
- 设备遥测上报
- 指数退避重试机制

API 端点对照（SaaS 后端 FastAPI v1）::

    POST   /api/v1/auth/login              → 登录获取 JWT
    POST   /api/v1/auth/refresh             → 刷新 Token
    GET    /api/v1/inspections/             → 拉取巡检任务
    POST   /api/v1/inspections/             → 上传巡检记录
    POST   /api/v1/inspections/{id}/photos  → 上传巡检照片
    POST   /api/v1/inspections/{id}/detect → 触发云端 AI 检测
    POST   /api/v1/rag/query               → RAG 问答
    POST   /api/v1/devices/telemetry        → 遥测上报
    POST   /api/v1/alerts/                  → 告警上报
    POST   /api/v1/ota/status               → OTA 状态上报
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ── 同步优先级（离线缓存排序） ─────────────────────────────────
# 告警 > 照片 > 检测 > RAG > 遥测
SYNC_PRIORITY = {
    "alert": 0,
    "photo": 1,
    "detection": 2,
    "rag": 3,
    "telemetry": 4,
    "inspection": 5,
}


class SyncStatus(Enum):
    PENDING = "pending"
    SYNCING = "syncing"
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"


@dataclass
class SaasApiConfig:
    """SaaS API 客户端配置。"""

    base_url: str = "https://saas.railway-inspection.com/api/v1"
    username: str = ""
    password: str = ""
    tenant_code: str = "default"
    # JWT
    token: str = ""
    refresh_token: str = ""
    token_expires_at: float = 0.0
    token_ttl_buffer: int = 300  # 提前 5 分钟刷新
    # HTTP
    timeout: float = 30.0
    connect_timeout: float = 10.0
    # 重试
    max_retries: int = 5
    retry_base_delay: float = 1.0
    retry_max_delay: float = 120.0
    retry_backoff_factor: float = 2.0
    # 重试状态码
    retry_status_codes: frozenset = frozenset(
        {408, 425, 429, 500, 502, 503, 504}
    )
    # 照片分块
    photo_chunk_size: int = 5 * 1024 * 1024  # 5MB
    # RAG
    rag_max_tokens: int = 512
    rag_timeout: float = 60.0


class SaasApiError(Exception):
    """SaaS API 调用异常。"""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SaasApiClient:
    """SaaS 后端异步 REST API 客户端。

    特性：
    - httpx AsyncClient 连接池复用
    - JWT 自动认证与刷新
    - 指数退避重试（可配置状态码）
    - 照片分块上传
    - RAG 远程回退
    - 全异步 API

    使用示例::

        async with SaasApiClient(config) as client:
            await client.login()
            tasks = await client.fetch_inspection_tasks(project_id="...")
            await client.upload_photo(inspection_id="...", file_path="/sd/photos/1.jpg")
    """

    def __init__(self, config: SaasApiConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._token_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()

    # ── 异步上下文管理 ────────────────────────────────────────────

    async def __aenter__(self) -> SaasApiClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def connect(self) -> None:
        """创建 httpx 异步客户端。"""
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=httpx.Timeout(
                timeout=self._config.timeout,
                connect=self._config.connect_timeout,
            ),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60,
            ),
            headers={"Content-Type": "application/json"},
        )
        logger.info("saas_client_connected", base_url=self._config.base_url)

    async def close(self) -> None:
        """关闭客户端连接。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("saas_client_closed")

    # ── 认证 ──────────────────────────────────────────────────────

    async def login(self) -> bool:
        """登录获取 JWT。"""
        async with self._login_lock:
            if self._is_token_valid():
                return True
            assert self._client is not None
            try:
                resp = await self._client.post(
                    "/auth/login",
                    json={
                        "username": self._config.username,
                        "password": self._config.password,
                        "tenant_code": self._config.tenant_code,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._config.token = data.get("access_token", "")
                self._config.refresh_token = data.get("refresh_token", "")
                expires_in = data.get("expires_in", 3600)
                self._config.token_expires_at = time.time() + expires_in
                logger.info(
                    "saas_login_success",
                    username=self._config.username,
                    expires_in=expires_in,
                )
                return True
            except httpx.HTTPError as exc:
                logger.error("saas_login_failed", error=str(exc))
                raise SaasApiError(f"Login failed: {exc}") from exc

    async def _refresh_token(self) -> bool:
        """刷新 JWT。"""
        if not self._config.refresh_token:
            return await self.login()
        assert self._client is not None
        try:
            resp = await self._client.post(
                "/auth/refresh",
                json={"refresh_token": self._config.refresh_token},
            )
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data.get("access_token", "")
            expires_in = data.get("expires_in", 3600)
            self._config.token_expires_at = time.time() + expires_in
            logger.info("saas_token_refreshed", expires_in=expires_in)
            return True
        except httpx.HTTPError as exc:
            logger.warning("saas_token_refresh_failed, retrying login", error=str(exc))
            return await self.login()

    def _is_token_valid(self) -> bool:
        """检查 Token 是否有效（含提前刷新缓冲）。"""
        return (
            bool(self._config.token)
            and time.time() < self._config.token_expires_at - self._config.token_ttl_buffer
        )

    async def _ensure_auth(self) -> None:
        """确保请求前 Token 有效。"""
        if not self._is_token_valid():
            async with self._token_lock:
                if not self._is_token_valid():
                    if self._config.refresh_token:
                        await self._refresh_token()
                    else:
                        await self.login()

    def _auth_headers(self) -> dict[str, str]:
        """构建认证请求头。"""
        headers: dict[str, str] = {}
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        return headers

    # ── 核心请求方法（带重试） ─────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        files: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
        is_retry_able: bool = True,
    ) -> httpx.Response:
        """执行 HTTP 请求，带认证和指数退避重试。

        :param is_retry_able: 是否启用重试（认证请求本身不重试）
        """
        await self._ensure_auth()
        assert self._client is not None

        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1 if is_retry_able else 1):
            try:
                resp = await self._client.request(
                    method,
                    url,
                    json=json,
                    data=data,
                    files=files,
                    headers=headers,
                    timeout=timeout or self._config.timeout,
                )
                # 401 → 尝试刷新 Token 后重试一次
                if resp.status_code == 401 and attempt == 0:
                    logger.warning("saas_401_unauthorized, refreshing token")
                    refreshed = await self._refresh_token()
                    if refreshed:
                        headers = self._auth_headers()
                        if extra_headers:
                            headers.update(extra_headers)
                        continue
                    raise SaasApiError("Authentication failed after token refresh", 401)

                # 可重试状态码
                if (
                    is_retry_able
                    and resp.status_code in self._config.retry_status_codes
                    and attempt < self._config.max_retries
                ):
                    delay = min(
                        self._config.retry_base_delay
                        * (self._config.retry_backoff_factor ** attempt),
                        self._config.retry_max_delay,
                    )
                    logger.warning(
                        "saas_request_retry",
                        method=method,
                        url=url,
                        status=resp.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    body = None
                    try:
                        body = resp.json()
                    except Exception:  # noqa: BLE001
                        body = resp.text
                    raise SaasApiError(
                        f"HTTP {resp.status_code}: {url}",
                        status_code=resp.status_code,
                        body=body,
                    )
                return resp

            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.ConnectTimeout,
            ) as exc:
                last_exc = exc
                if not is_retry_able or attempt >= self._config.max_retries:
                    raise SaasApiError(f"Network error: {exc}") from exc
                delay = min(
                    self._config.retry_base_delay
                    * (self._config.retry_backoff_factor ** attempt),
                    self._config.retry_max_delay,
                )
                logger.warning(
                    "saas_network_retry",
                    method=method,
                    url=url,
                    error=str(exc),
                    attempt=attempt + 1,
                    delay=delay,
                )
                await asyncio.sleep(delay)

        # 所有重试耗尽
        raise SaasApiError(
            f"Max retries ({self._config.max_retries}) exceeded for {url}"
        ) from last_exc

    # ── 巡检同步 ──────────────────────────────────────────────────

    async def fetch_inspection_tasks(
        self,
        project_id: str,
        status: str = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """从 SaaS 拉取巡检任务列表。

        :param project_id: 项目 ID
        :param status: 任务状态过滤（pending/in_progress/completed）
        :param limit: 分页大小
        :param offset: 偏移量
        :return: 巡检任务列表
        """
        resp = await self._request(
            "GET",
            "/inspections/",
            extra_headers={"X-Project-ID": project_id},
            params={"status": status, "limit": limit, "offset": offset},
        )
        data = resp.json()
        tasks = data if isinstance(data, list) else data.get("items", data.get("data", []))
        logger.info("inspection_tasks_fetched", count=len(tasks), project_id=project_id)
        return tasks

    async def upload_inspection(
        self, inspection: dict[str, Any]) -> dict[str, Any]:
        """上传巡检记录到 SaaS。

        :param inspection: 巡检记录数据
        :return: SaaS 返回的巡检记录（含 UUID）
        """
        resp = await self._request(
            "POST",
            "/inspections/",
            json=inspection,
        )
        result = resp.json()
        logger.info(
            "inspection_uploaded",
            inspection_id=result.get("id"),
            status=inspection.get("status"),
        )
        return result

    async def update_inspection_status(
        self,
        inspection_id: str,
        status: str,
        notes: str = "",
        ai_findings: dict | None = None,
    ) -> dict[str, Any]:
        """更新巡检任务状态。"""
        payload: dict[str, Any] = {"status": status, "notes": notes}
        if ai_findings:
            payload["ai_findings"] = ai_findings
        resp = await self._request(
            "PATCH",
            f"/inspections/{inspection_id}",
            json=payload,
        )
        return resp.json()

    # ── 照片上传 ──────────────────────────────────────────────────

    async def upload_photo(
        self,
        inspection_id: str,
        file_path: str,
        mime_type: str = "image/jpeg",
        taken_at: str | None = None,
    ) -> dict[str, Any]:
        """上传巡检照片（分块上传）。

        :param inspection_id: 巡检 ID
        :param file_path: 本地照片文件路径
        :param mime_type: MIME 类型
        :param taken_at: 拍照时间 ISO 格式
        :return: SaaS 返回的照片记录（含 MinIO 对象名）
        """
        import os

        file_size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)

        # 大文件使用分块上传，小文件直接 multipart
        if file_size <= self._config.photo_chunk_size:
            return await self._upload_photo_single(
                inspection_id, file_path, filename, mime_type, taken_at, file_size
            )
        return await self._upload_photo_chunked(
            inspection_id, file_path, filename, mime_type, taken_at, file_size
        )

    async def _upload_photo_single(
        self,
        inspection_id: str,
        file_path: str,
        filename: str,
        mime_type: str,
        taken_at: str | None,
        file_size: int,
    ) -> dict[str, Any]:
        """小文件直接 multipart 上传。"""
        with open(file_path, "rb") as f:
            files = {"file": (filename, f, mime_type)}
            extra: dict[str, str] = {}
            if taken_at:
                extra["X-Taken-At"] = taken_at
            resp = await self._request(
                "POST",
                f"/inspections/{inspection_id}/photos",
                files=files,
                extra_headers={**extra, "Content-Type": None} if extra else None,
            )
        result = resp.json()
        logger.info(
            "photo_uploaded_single",
            inspection_id=inspection_id,
            filename=filename,
            size=file_size,
            object_name=result.get("minio_object_name"),
        )
        return result

    async def _upload_photo_chunked(
        self,
        inspection_id: str,
        file_path: str,
        filename: str,
        mime_type: str,
        taken_at: str | None,
        file_size: int,
    ) -> dict[str, Any]:
        """大文件分块上传（initiate → upload chunks → complete）。"""
        # 1. 初始化分块上传
        resp = await self._request(
            "POST",
            f"/inspections/{inspection_id}/photos/chunked/initiate",
            json={
                "filename": filename,
                "mime_type": mime_type,
                "file_size": file_size,
                "taken_at": taken_at,
            },
        )
        upload_data = resp.json()
        upload_id = upload_data["upload_id"]
        total_chunks = upload_data["total_chunks"]

        # 2. 逐块上传
        chunk_size = self._config.photo_chunk_size
        with open(file_path, "rb") as f:
            for chunk_idx in range(total_chunks):
                chunk_data = f.read(chunk_size)
                if not chunk_data:
                    break
                files = {
                    "chunk": (f"chunk_{chunk_idx}", chunk_data, "application/octet-stream")
                }
                await self._request(
                    "POST",
                    f"/inspections/{inspection_id}/photos/chunked/{upload_id}",
                    files=files,
                    extra_headers={"X-Chunk-Index": str(chunk_idx)},
                )
                logger.debug(
                    "photo_chunk_uploaded",
                    inspection_id=inspection_id,
                    chunk=chunk_idx + 1,
                    total=total_chunks,
                )

        # 3. 完成上传
        resp = await self._request(
            "POST",
            f"/inspections/{inspection_id}/photos/chunked/{upload_id}/complete",
        )
        result = resp.json()
        logger.info(
            "photo_uploaded_chunked",
            inspection_id=inspection_id,
            filename=filename,
            size=file_size,
            chunks=total_chunks,
            object_name=result.get("minio_object_name"),
        )
        return result

    # ── RAG 回退 ──────────────────────────────────────────────────

    async def rag_fallback_query(
        self,
        question: str,
        project_id: str,
        conversation_id: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """本地 RAG 失败时，回退到 SaaS Dify 远程 RAG。

        :param question: 用户问题
        :param project_id: 项目 ID（用于知识库隔离）
        :param conversation_id: 会话 ID（多轮对话）
        :param max_tokens: 最大生成 Token 数
        :return: {"answer": str, "sources": list, "confidence": float}
        """
        resp = await self._request(
            "POST",
            "/rag/query",
            json={
                "question": question,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "max_tokens": max_tokens or self._config.rag_max_tokens,
            },
            timeout=self._config.rag_timeout,
        )
        result = resp.json()
        logger.info(
            "rag_fallback_success",
            question_len=len(question),
            answer_len=len(result.get("answer", "")),
            sources_count=len(result.get("sources", [])),
        )
        return result

    # ── 告警上报 ──────────────────────────────────────────────────

    async def report_alert(
        self,
        project_id: str,
        inspection_id: str | None,
        alert_type: str,
        severity: str,
        description: str,
        image_url: str | None = None,
    ) -> dict[str, Any]:
        """上报告警到 SaaS（最高同步优先级）。"""
        payload = {
            "project_id": project_id,
            "inspection_id": inspection_id,
            "type": alert_type,
            "severity": severity,
            "description": description,
        }
        if image_url:
            payload["image_url"] = image_url
        resp = await self._request("POST", "/alerts/", json=payload)
        result = resp.json()
        logger.info(
            "alert_reported",
            alert_type=alert_type,
            severity=severity,
            alert_id=result.get("id"),
        )
        return result

    # ── 遥测上报 ──────────────────────────────────────────────────

    async def report_telemetry(
        self,
        device_id: str,
        telemetry: dict[str, Any],
    ) -> dict[str, Any]:
        """批量上报设备遥测数据。"""
        resp = await self._request(
            "POST",
            "/devices/telemetry",
            json={
                "device_id": device_id,
                "telemetry": telemetry,
                "timestamp": int(time.time()),
            },
        )
        return resp.json()

    # ── OTA 状态 ──────────────────────────────────────────────────

    async def report_ota_status(
        self,
        device_id: str,
        upgrade_record_id: str,
        status: str,
        progress: int,
        error_message: str = "",
    ) -> dict[str, Any]:
        """上报 OTA 升级状态。"""
        resp = await self._request(
            "POST",
            "/ota/status",
            json={
                "device_id": device_id,
                "upgrade_record_id": upgrade_record_id,
                "status": status,
                "progress": progress,
                "error_message": error_message,
            },
        )
        return resp.json()

    # ── AI 检测回退 ───────────────────────────────────────────────

    async def trigger_cloud_detection(
        self,
        inspection_id: str,
    ) -> dict[str, Any]:
        """触发 SaaS 云端 AI 检测（本地 YOLO 失败时回退）。"""
        resp = await self._request("POST", f"/inspections/{inspection_id}/detect")
        return resp.json()

    # ── 健康检查 ──────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """检查 SaaS 后端是否可达。"""
        try:
            assert self._client is not None
            resp = await self._client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
