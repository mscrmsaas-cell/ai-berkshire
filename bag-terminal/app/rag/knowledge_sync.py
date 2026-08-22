"""
知识同步模块 — 从 SaaS Dify 增量同步知识库

功能:
    - 从 SaaS 后端拉取知识库文档列表 (含 content_hash)
    - 基于 content_hash 增量同步 (仅同步变更/新增文档)
    - 向量化并写入本地 Qdrant
    - 定期同步 (可配置间隔)
    - 同步状态追踪

同步策略:
    1. 拉取远端文档清单 (id, content_hash, updated_at)
    2. 与本地已有 content_hash 集合对比
    3. 新增: 下载内容 → 向量化 → 写入 Qdrant
    4. 更新: 删除旧文档 → 写入新文档
    5. 删除: 远端已删除的文档从本地移除
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from app.config import Settings
from app.rag.embedding_model import EmbeddingModel
from app.rag.vector_store import VectorStore

logger = structlog.get_logger(__name__)

# httpx 延迟导入
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore


@dataclass
class RemoteDocument:
    """远端文档信息"""
    id: str
    title: str = ""
    content: str = ""
    content_hash: str = ""
    source: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    """同步结果"""
    total_remote: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


class KnowledgeSync:
    """
    知识库增量同步器

    从 SaaS 后端 Dify 知识库拉取文档, 向量化后写入本地 Qdrant。
    """

    def __init__(
        self,
        settings: Settings,
        embedding_model: EmbeddingModel,
        vector_store: VectorStore,
    ):
        self.settings = settings
        self.rag_config = settings.rag
        self.saas_config = settings.saas

        self._embedding = embedding_model
        self._vector_store = vector_store

        # 同步状态
        self._last_sync_at: datetime | None = None
        self._last_sync_result: SyncResult | None = None
        self._is_syncing: bool = False
        self._sync_lock = asyncio.Lock()

        # 本地 content_hash 索引 (避免每次全量查询 Qdrant)
        self._local_hashes: set[str] = set()
        self._local_hash_to_id: dict[str, str] = {}  # content_hash -> doc_id

    # -------------------------------------------------------------------
    # 同步入口
    # -------------------------------------------------------------------

    async def sync(self) -> SyncResult:
        """
        执行增量同步

        Returns:
            SyncResult: 同步结果
        """
        async with self._sync_lock:
            if self._is_syncing:
                logger.warning("knowledge_sync.already_syncing")
                return SyncResult()

            self._is_syncing = True
            start = time.perf_counter()

            result = SyncResult()

            try:
                # 1. 拉取远端文档清单
                remote_docs = await self._fetch_remote_documents()
                result.total_remote = len(remote_docs)

                # 2. 加载本地已有 content_hash
                await self._load_local_hashes()

                # 3. 增量对比
                remote_hashes = {doc.content_hash for doc in remote_docs if doc.content_hash}

                # 需要新增/更新的
                to_add = [
                    doc for doc in remote_docs
                    if doc.content_hash and doc.content_hash not in self._local_hashes
                ]

                # 需要删除的 (本地有但远端没有)
                to_delete = self._local_hashes - remote_hashes

                # 4. 执行同步
                for doc in to_add:
                    try:
                        success = await self._sync_document(doc)
                        if success:
                            result.added += 1
                        else:
                            result.failed += 1
                    except Exception as exc:
                        result.failed += 1
                        result.errors.append(f"添加文档 {doc.id} 失败: {str(exc)}")
                        logger.error(
                            "knowledge_sync.add_error",
                            doc_id=doc.id,
                            error=str(exc),
                        )

                for content_hash in to_delete:
                    try:
                        deleted = await self._vector_store.delete_by_hash(content_hash)
                        if deleted:
                            result.deleted += deleted
                            self._local_hashes.discard(content_hash)
                            self._local_hash_to_id.pop(content_hash, None)
                    except Exception as exc:
                        result.failed += 1
                        result.errors.append(f"删除 hash {content_hash[:8]} 失败: {str(exc)}")

                result.skipped = result.total_remote - result.added - result.failed

            except Exception as exc:
                result.errors.append(f"同步异常: {str(exc)}")
                logger.error("knowledge_sync.error", error=str(exc))
            finally:
                result.elapsed_seconds = time.perf_counter() - start
                self._last_sync_at = datetime.now(timezone.utc)
                self._last_sync_result = result
                self._is_syncing = False

            logger.info(
                "knowledge_sync.completed",
                total=result.total_remote,
                added=result.added,
                deleted=result.deleted,
                failed=result.failed,
                elapsed=round(result.elapsed_seconds, 2),
            )

            return result

    # -------------------------------------------------------------------
    # 远端拉取
    # -------------------------------------------------------------------

    async def _fetch_remote_documents(self) -> list[RemoteDocument]:
        """从 SaaS Dify 拉取文档清单"""
        if not HTTPX_AVAILABLE:
            logger.warning("knowledge_sync.httpx_not_available")
            return []

        url = self.rag_config.saas_dify_url
        if not url:
            logger.warning("knowledge_sync.no_dify_url")
            return []

        try:
            async with httpx.AsyncClient(timeout=self.saas_config.api_timeout) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.error(
                        "knowledge_sync.fetch_failed",
                        status=resp.status_code,
                    )
                    return []

                data = resp.json()
                documents = data.get("documents", data) if isinstance(data, dict) else data

                remote_docs = []
                for item in documents:
                    content = item.get("content", "")
                    content_hash = item.get("content_hash", "")
                    if not content_hash and content:
                        content_hash = self._compute_hash(content)

                    remote_docs.append(RemoteDocument(
                        id=item.get("id", ""),
                        title=item.get("title", ""),
                        content=content,
                        content_hash=content_hash,
                        source=item.get("source", "dify"),
                        updated_at=item.get("updated_at", ""),
                        metadata=item.get("metadata", {}),
                    ))

                return remote_docs

        except Exception as exc:
            logger.error("knowledge_sync.fetch_error", error=str(exc))
            return []

    async def _fetch_document_content(self, doc_id: str) -> str:
        """拉取单个文档的完整内容"""
        if not HTTPX_AVAILABLE:
            return ""

        url = f"{self.rag_config.saas_dify_url}/{doc_id}/content"
        try:
            async with httpx.AsyncClient(timeout=self.saas_config.api_timeout) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                return data.get("content", "")
        except Exception as exc:
            logger.error("knowledge_sync.fetch_content_error", doc_id=doc_id, error=str(exc))
            return ""

    # -------------------------------------------------------------------
    # 文档同步
    # -------------------------------------------------------------------

    async def _sync_document(self, doc: RemoteDocument) -> bool:
        """同步单个文档到 Qdrant"""
        # 如果内容为空, 尝试拉取
        if not doc.content and doc.id:
            doc.content = await self._fetch_document_content(doc.id)

        if not doc.content:
            logger.warning("knowledge_sync.empty_content", doc_id=doc.id)
            return False

        # 截断超长文档
        max_chars = self.rag_config.max_context_chars * 10  # 允许较长文档
        if len(doc.content) > max_chars:
            doc.content = doc.content[:max_chars]

        # 向量化
        vector = await self._embedding.embed(doc.content)
        if vector is None or len(vector) == 0:
            logger.error("knowledge_sync.embed_failed", doc_id=doc.id)
            return False

        # 写入 Qdrant
        doc_id = await self._vector_store.insert(
            content=doc.content,
            vector=vector,
            metadata={
                "title": doc.title,
                "remote_id": doc.id,
                "updated_at": doc.updated_at,
                **doc.metadata,
            },
            content_hash=doc.content_hash,
            source=doc.source,
        )

        if doc_id:
            self._local_hashes.add(doc.content_hash)
            self._local_hash_to_id[doc.content_hash] = doc_id
            logger.debug(
                "knowledge_sync.doc_synced",
                doc_id=doc.id,
                hash=doc.content_hash[:8],
            )
            return True

        return False

    # -------------------------------------------------------------------
    # 辅助
    # -------------------------------------------------------------------

    @staticmethod
    def _compute_hash(content: str) -> str:
        """计算内容的 SHA-256 哈希"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def _load_local_hashes(self) -> None:
        """加载本地已有的 content_hash 集合"""
        # 简化实现: 如果已加载则跳过
        # 完整实现需要遍历 Qdrant payload
        if self._local_hashes:
            return

        # 通过 count 确认
        count = await self._vector_store.count()
        if count == 0:
            return

        # TODO: 批量 scroll 获取所有 payload 中的 content_hash
        # 当前简化: 清空缓存, 下次同步时通过 add 逐步构建
        self._local_hashes = set()
        self._local_hash_to_id = {}

    # -------------------------------------------------------------------
    # 状态查询
    # -------------------------------------------------------------------

    @property
    def is_syncing(self) -> bool:
        return self._is_syncing

    @property
    def last_sync_at(self) -> datetime | None:
        return self._last_sync_at

    def get_status(self) -> dict[str, Any]:
        """获取同步状态"""
        return {
            "is_syncing": self._is_syncing,
            "last_sync_at": self._last_sync_at.isoformat() if self._last_sync_at else None,
            "local_doc_count": len(self._local_hashes),
            "last_result": {
                "total_remote": self._last_sync_result.total_remote if self._last_sync_result else 0,
                "added": self._last_sync_result.added if self._last_sync_result else 0,
                "updated": self._last_sync_result.updated if self._last_sync_result else 0,
                "deleted": self._last_sync_result.deleted if self._last_sync_result else 0,
                "failed": self._last_sync_result.failed if self._last_sync_result else 0,
                "elapsed": round(self._last_sync_result.elapsed_seconds, 2) if self._last_sync_result else 0,
            } if self._last_sync_result else None,
        }
