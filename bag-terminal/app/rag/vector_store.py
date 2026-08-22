"""
向量存储模块 — Qdrant 本地模式

功能:
    - 集合创建 (768 维)
    - 文档插入 (向量 + metadata)
    - 相似度搜索 (Cosine)
    - 批量操作

使用 Qdrant 本地模式 (嵌入式, 无需独立服务进程)。
存储路径: data/qdrant/
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

# Qdrant 客户端延迟导入
try:
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import (
        Distance,
        PointStruct,
        VectorParams,
        Filter,
        FieldCondition,
        MatchValue,
    )
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None  # type: ignore


@dataclass
class VectorDocument:
    """向量文档"""
    id: str
    content: str
    vector: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    source: str = ""
    score: float = 0.0  # 搜索结果得分


@dataclass
class SearchResult:
    """搜索结果"""
    documents: list[VectorDocument] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0


class VectorStore:
    """
    Qdrant 本地模式向量存储

    嵌入式运行, 数据持久化到本地磁盘。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.rag_config = settings.rag

        self._collection_name: str = self.rag_config.collection_name
        self._embedding_dim: int = self.rag_config.embedding_dim
        self._qdrant_path: str = self.rag_config.qdrant_path

        self._client: Any = None  # QdrantClient
        self._is_initialized: bool = False

    # -------------------------------------------------------------------
    # 初始化
    # -------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 Qdrant 本地客户端"""
        if not QDRANT_AVAILABLE:
            logger.warning("vector_store.qdrant_not_available")
            return

        # 确保存储目录存在
        Path(self._qdrant_path).mkdir(parents=True, exist_ok=True)

        # 创建本地模式客户端
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._init_client_sync,
        )

        self._is_initialized = True
        logger.info(
            "vector_store.initialized",
            path=self._qdrant_path,
            collection=self._collection_name,
            dim=self._embedding_dim,
        )

    def _init_client_sync(self) -> None:
        """同步初始化客户端"""
        self._client = QdrantClient(path=self._qdrant_path)

        # 检查/创建集合
        collections = self._client.get_collections()
        collection_names = [c.name for c in collections.collections]

        if self._collection_name not in collection_names:
            self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config=VectorParams(
                    size=self._embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(
                "vector_store.collection_created",
                name=self._collection_name,
                dim=self._embedding_dim,
            )

    async def shutdown(self) -> None:
        """关闭客户端"""
        if self._client:
            self._client.close()
            self._client = None
        self._is_initialized = False
        logger.info("vector_store.shutdown")

    # -------------------------------------------------------------------
    # 文档操作
    # -------------------------------------------------------------------

    async def insert(
        self,
        content: str,
        vector: np.ndarray | list[float],
        metadata: dict[str, Any] | None = None,
        doc_id: str | None = None,
        content_hash: str = "",
        source: str = "",
    ) -> str:
        """
        插入单条文档

        Args:
            content: 文本内容
            vector: 嵌入向量 (768 维)
            metadata: 附加元数据
            doc_id: 文档 ID (None 自动生成)
            content_hash: 内容哈希 (用于增量同步去重)
            source: 来源

        Returns: 文档 ID
        """
        if not self._is_initialized or not self._client:
            logger.warning("vector_store.not_initialized")
            return ""

        doc_id = doc_id or str(uuid.uuid4())
        vector_list = vector.tolist() if isinstance(vector, np.ndarray) else list(vector)

        payload = {
            "content": content,
            "content_hash": content_hash,
            "source": source,
            **(metadata or {}),
        }

        point = PointStruct(
            id=doc_id,
            vector=vector_list,
            payload=payload,
        )

        await asyncio.get_event_loop().run_in_executor(
            None,
            self._client.upsert,
            self._collection_name,
            [point],
        )

        logger.debug(
            "vector_store.inserted",
            doc_id=doc_id,
            content_len=len(content),
        )
        return doc_id

    async def insert_batch(
        self,
        documents: list[VectorDocument],
    ) -> list[str]:
        """
        批量插入文档

        Args:
            documents: VectorDocument 列表 (需含 vector)

        Returns: 文档 ID 列表
        """
        if not self._is_initialized or not self._client:
            return []

        if not documents:
            return []

        points = []
        for doc in documents:
            doc_id = doc.id or str(uuid.uuid4())
            payload = {
                "content": doc.content,
                "content_hash": doc.content_hash,
                "source": doc.source,
                **doc.metadata,
            }
            points.append(PointStruct(
                id=doc_id,
                vector=doc.vector,
                payload=payload,
            ))

        await asyncio.get_event_loop().run_in_executor(
            None,
            self._client.upsert,
            self._collection_name,
            points,
        )

        logger.info("vector_store.batch_inserted", count=len(points))
        return [p.id for p in points]

    async def search(
        self,
        query_vector: np.ndarray | list[float],
        top_k: int = 5,
        score_threshold: float = 0.0,
        filters: dict[str, Any] | None = None,
    ) -> SearchResult:
        """
        相似度搜索

        Args:
            query_vector: 查询向量
            top_k: 返回 Top-K 结果
            score_threshold: 得分阈值 (低于此值不返回)
            filters: 元数据过滤条件

        Returns:
            SearchResult: 搜索结果
        """
        if not self._is_initialized or not self._client:
            return SearchResult()

        import time as _time
        start = _time.perf_counter()

        vector_list = query_vector.tolist() if isinstance(query_vector, np.ndarray) else list(query_vector)

        # 构建过滤器
        qdrant_filter = None
        if filters:
            conditions = []
            for key, value in filters.items():
                conditions.append(
                    FieldCondition(
                        key=key,
                        match=MatchValue(value=value),
                    )
                )
            qdrant_filter = Filter(must=conditions) if conditions else None

        # 搜索
        results = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.search(
                collection_name=self._collection_name,
                query_vector=vector_list,
                limit=top_k,
                score_threshold=score_threshold,
                query_filter=qdrant_filter,
            ),
        )

        elapsed = (_time.perf_counter() - start) * 1000

        documents = []
        for point in results:
            payload = point.payload or {}
            documents.append(VectorDocument(
                id=str(point.id),
                content=payload.get("content", ""),
                metadata={k: v for k, v in payload.items() if k not in ("content", "content_hash", "source")},
                content_hash=payload.get("content_hash", ""),
                source=payload.get("source", ""),
                score=point.score,
            ))

        return SearchResult(
            documents=documents,
            total=len(documents),
            elapsed_ms=elapsed,
        )

    # -------------------------------------------------------------------
    # 删除/查询
    # -------------------------------------------------------------------

    async def delete(self, doc_id: str) -> bool:
        """删除单个文档"""
        if not self._is_initialized or not self._client:
            return False

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.delete(
                collection_name=self._collection_name,
                points_selector=[doc_id],
            ),
        )
        return True

    async def delete_by_hash(self, content_hash: str) -> int:
        """按 content_hash 删除文档"""
        if not self._is_initialized or not self._client:
            return 0

        # 查找该 hash 的所有点
        results = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="content_hash",
                            match=MatchValue(value=content_hash),
                        )
                    ]
                ),
                limit=1000,
            ),
        )

        points = results[0] if isinstance(results, tuple) else results
        ids = [str(p.id) for p in points]
        if ids:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.delete(
                    collection_name=self._collection_name,
                    points_selector=ids,
                ),
            )
        return len(ids)

    async def count(self) -> int:
        """获取文档总数"""
        if not self._is_initialized or not self._client:
            return 0
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.count(
                collection_name=self._collection_name,
                exact=True,
            ),
        )
        return result.count

    async def get_by_content_hash(self, content_hash: str) -> VectorDocument | None:
        """按 content_hash 查询文档"""
        if not self._is_initialized or not self._client:
            return None

        results = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="content_hash",
                            match=MatchValue(value=content_hash),
                        )
                    ]
                ),
                limit=1,
            ),
        )

        points = results[0] if isinstance(results, tuple) else results
        if not points:
            return None

        p = points[0]
        payload = p.payload or {}
        return VectorDocument(
            id=str(p.id),
            content=payload.get("content", ""),
            content_hash=payload.get("content_hash", ""),
            source=payload.get("source", ""),
        )

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
