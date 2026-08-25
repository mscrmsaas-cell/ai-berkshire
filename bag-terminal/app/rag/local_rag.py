"""
本地 RAG 引擎 — 检索增强生成

流程:
    用户问题 → 向量化 → Top-5 检索 → 组装 Prompt → LLM 生成 (≤512 tokens) → 返回答案 + 来源

组件:
    - EmbeddingModel: BGE-small-zh 文本向量化
    - VectorStore: Qdrant 本地相似度搜索
    - LlmInference: Phi-3-mini Q4 文本生成
    - KnowledgeSync: 知识库增量同步
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from app.config import Settings
from app.rag.embedding_model import EmbeddingModel
from app.rag.knowledge_sync import KnowledgeSync
from app.rag.llm_inference import LlmInference
from app.rag.vector_store import VectorStore, SearchResult

logger = structlog.get_logger(__name__)


class LocalRAG:
    """
    本地 RAG 引擎

    整合嵌入、检索、生成三阶段, 提供端到端问答能力。
    所有推理在本地 CPU 运行, 无需网络 (知识同步除外)。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.rag_config = settings.rag

        # 组件
        self._embedding = EmbeddingModel(settings)
        self._vector_store = VectorStore(settings)
        self._llm = LlmInference(settings)
        self._knowledge_sync = KnowledgeSync(
            settings,
            self._embedding,
            self._vector_store,
        )

        # 状态
        self._is_ready: bool = False
        self._query_count: int = 0
        self._sync_task: asyncio.Task | None = None

    # -------------------------------------------------------------------
    # 初始化
    # -------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 RAG 引擎"""
        logger.info("rag.initializing")

        # 并行初始化各组件
        await asyncio.gather(
            self._vector_store.initialize(),
            self._embedding.load(),
            self._llm.load(),
            return_exceptions=True,
        )

        # 检查组件状态
        embedding_ready = self._embedding.is_loaded
        vector_ready = self._vector_store.is_initialized
        llm_ready = self._llm.is_loaded

        self._is_ready = embedding_ready or vector_ready or llm_ready

        logger.info(
            "rag.initialized",
            embedding=embedding_ready,
            vector_store=vector_ready,
            llm=llm_ready,
        )

        # 如果有模型, 执行首次知识同步
        if embedding_ready and vector_ready:
            try:
                result = await self._knowledge_sync.sync()
                logger.info(
                    "rag.initial_sync_done",
                    added=result.added,
                    total=result.total_remote,
                )
            except Exception as exc:
                logger.warning("rag.initial_sync_failed", error=str(exc))

        # 启动定期同步
        if self.rag_config.sync_interval > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def shutdown(self) -> None:
        """关闭 RAG 引擎"""
        logger.info("rag.shutting_down")

        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        # 并行关闭
        await asyncio.gather(
            self._embedding.unload(),
            self._vector_store.shutdown(),
            self._llm.unload(),
            return_exceptions=True,
        )

        self._is_ready = False
        logger.info("rag.stopped")

    # -------------------------------------------------------------------
    # 问答接口
    # -------------------------------------------------------------------

    async def query(
        self,
        question: str,
        top_k: int | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """
        RAG 问答

        Args:
            question: 用户问题
            top_k: 检索 Top-K 文档 (None 用配置默认值 5)
            max_tokens: LLM 最大生成 token 数 (None 用配置默认值 512)

        Returns:
            {
                "question": "原问题",
                "answer": "回答文本",
                "sources": ["来源1", "来源2", ...],
                "confidence": 0.0-1.0,
                "retrieval_count": 5,
                "generation_tokens": 123,
                "elapsed_ms": 1234.5,
            }
        """
        import time as _time
        start = _time.perf_counter()

        self._query_count += 1
        top_k = top_k or self.rag_config.top_k
        max_tokens = max_tokens or self.settings.models.llm.max_tokens

        # 1. 向量化问题
        query_vector = await self._embedding.embed(question)

        # 2. 检索相关文档
        search_result: SearchResult
        if self._embedding.is_loaded and self._vector_store.is_initialized:
            search_result = await self._vector_store.search(
                query_vector=query_vector,
                top_k=top_k,
                score_threshold=self.rag_config.score_threshold,
            )
        else:
            search_result = SearchResult()

        # 3. 组装上下文
        context_docs = [doc.content for doc in search_result.documents if doc.content]
        sources = [
            doc.source or doc.metadata.get("title", f"文档_{doc.id[:8]}")
            for doc in search_result.documents
        ]

        # 截断上下文 (控制 prompt 长度)
        max_context_chars = self.rag_config.max_context_chars
        truncated_contexts = []
        total_chars = 0
        for ctx in context_docs:
            if total_chars + len(ctx) > max_context_chars:
                remaining = max_context_chars - total_chars
                if remaining > 100:
                    truncated_contexts.append(ctx[:remaining] + "...")
                break
            truncated_contexts.append(ctx)
            total_chars += len(ctx)

        # 4. 构建 prompt 并生成
        if self._llm.is_loaded:
            prompt = self._llm.build_rag_prompt(
                question=question,
                context_docs=truncated_contexts,
            )
            gen_result = await self._llm.generate(
                prompt=prompt,
                max_tokens=max_tokens,
            )
            answer = gen_result.text
            generation_tokens = gen_result.tokens_generated
            confidence = self._calculate_confidence(search_result, gen_result)
        else:
            # LLM 未加载, 降级: 返回检索到的原文
            if truncated_contexts:
                answer = "根据参考资料:\n\n" + "\n\n".join(truncated_contexts[:3])
            else:
                answer = "RAG 引擎模型未加载, 无法生成回答。"
            generation_tokens = 0
            confidence = 0.0

        elapsed = (_time.perf_counter() - start) * 1000

        logger.info(
            "rag.query_completed",
            question_len=len(question),
            answer_len=len(answer),
            retrieved=search_result.total,
            tokens=generation_tokens,
            elapsed_ms=round(elapsed, 1),
        )

        return {
            "question": question,
            "answer": answer,
            "sources": sources,
            "confidence": round(confidence, 3),
            "retrieval_count": search_result.total,
            "retrieval_scores": [round(d.score, 4) for d in search_result.documents],
            "generation_tokens": generation_tokens,
            "elapsed_ms": round(elapsed, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def query_stream(
        self,
        question: str,
        top_k: int | None = None,
        max_tokens: int | None = None,
    ):
        """
        流式问答 (逐 token 输出)

        Yields:
            str: 生成文本片段
        """
        top_k = top_k or self.rag_config.top_k
        max_tokens = max_tokens or self.settings.models.llm.max_tokens

        # 1. 向量化 + 检索
        query_vector = await self._embedding.embed(question)
        search_result = await self._vector_store.search(
            query_vector=query_vector,
            top_k=top_k,
            score_threshold=self.rag_config.score_threshold,
        )

        context_docs = [doc.content for doc in search_result.documents if doc.content]

        # 2. 构建 prompt
        if not self._llm.is_loaded:
            yield "LLM 模型未加载。"
            return

        prompt = self._llm.build_rag_prompt(
            question=question,
            context_docs=context_docs,
        )

        # 3. 流式生成
        async for chunk in self._llm.generate_stream(
            prompt,
            max_tokens=max_tokens,
        ):
            yield chunk

    # -------------------------------------------------------------------
    # 知识同步 (v2.0: USB 有线导入, 替代云端同步)
    # -------------------------------------------------------------------

    async def sync_knowledge(self) -> int:
        """手动触发知识同步 (已禁用云端, 返回 0)"""
        # 云端同步已在 v2.0 等保架构中禁用
        # 如需导入知识, 请使用 import_from_usb()
        return 0

    async def import_from_usb(self) -> int:
        """
        从 USB 导入知识库文件 (PC 推送到 pc_import_dir 目录)

        流程:
            1. 扫描 pc_import_dir 目录中的 .txt / .json / .md 文件
            2. 逐文件读取内容 → BGE 向量化 → Qdrant 插入
            3. 导入完成后标记文件为 .imported

        Returns: 成功导入的文档数
        """
        import_path = Path(self.rag_config.pc_import_dir)
        if not import_path.exists():
            logger.warning("rag.import_dir_not_found", path=str(import_path))
            return 0

        if not self._embedding.is_loaded or not self._vector_store.is_initialized:
            logger.error("rag.not_initialized_for_import")
            return 0

        supported_extensions = {".txt", ".json", ".md", ".csv"}
        imported_count = 0

        for file_path in sorted(import_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in supported_extensions:
                continue
            if file_path.suffix == ".imported":
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                if not content.strip():
                    continue

                # 对于 JSON 文件, 提取文本字段
                if file_path.suffix == ".json":
                    import json
                    data = json.loads(content)
                    if isinstance(data, list):
                        # 文档列表: [{"content": "...", "source": "..."}]
                        for doc in data:
                            text = doc.get("content", "") if isinstance(doc, dict) else str(doc)
                            source = doc.get("source", file_path.name) if isinstance(doc, dict) else file_path.name
                            if text.strip():
                                doc_id = await self.add_document(text, source=source)
                                if doc_id:
                                    imported_count += 1
                    elif isinstance(data, dict):
                        text = data.get("content", content)
                        doc_id = await self.add_document(text, source=file_path.name)
                        if doc_id:
                            imported_count += 1
                else:
                    doc_id = await self.add_document(content, source=file_path.name)
                    if doc_id:
                        imported_count += 1

                # 标记为已导入
                imported_path = file_path.with_suffix(file_path.suffix + ".imported")
                file_path.rename(imported_path)

                logger.info("rag.document_imported", file=file_path.name)

            except Exception as exc:
                logger.error("rag.import_file_error", file=str(file_path), error=str(exc))

        logger.info("rag.usb_import_complete", imported=imported_count)
        return imported_count

    async def _sync_loop(self) -> None:
        """定期同步知识库 (v2.0: sync_interval=0 时禁用)"""
        if self.rag_config.sync_interval <= 0:
            logger.info("rag.sync_disabled", reason="sync_interval_is_zero")
            return

        while True:
            try:
                await asyncio.sleep(self.rag_config.sync_interval)
                await self._knowledge_sync.sync()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("rag.sync_loop_error", error=str(exc))
                await asyncio.sleep(60)

    # -------------------------------------------------------------------
    # 文档管理
    # -------------------------------------------------------------------

    async def add_document(
        self,
        content: str,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """手动添加文档到知识库"""
        if not self._embedding.is_loaded or not self._vector_store.is_initialized:
            return None

        vector = await self._embedding.embed(content)
        import hashlib
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        doc_id = await self._vector_store.insert(
            content=content,
            vector=vector,
            metadata=metadata or {},
            content_hash=content_hash,
            source=source,
        )

        logger.info("rag.document_added", doc_id=doc_id, content_len=len(content))
        return doc_id

    async def search_documents(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """仅检索文档 (不生成回答)"""
        if not self._embedding.is_loaded or not self._vector_store.is_initialized:
            return []

        query_vector = await self._embedding.embed(query)
        result = await self._vector_store.search(
            query_vector=query_vector,
            top_k=top_k,
        )

        return [
            {
                "content": doc.content[:200] + "..." if len(doc.content) > 200 else doc.content,
                "source": doc.source,
                "score": round(doc.score, 4),
            }
            for doc in result.documents
        ]

    # -------------------------------------------------------------------
    # 辅助
    # -------------------------------------------------------------------

    def _calculate_confidence(self, search_result: SearchResult, gen_result: Any) -> float:
        """
        计算回答置信度

        综合:
            - 检索得分 (Top-1 的 cosine 相似度)
            - 检索文档数
            - 生成 token 数
        """
        if not search_result.documents:
            return 0.1

        # Top-1 相似度
        top_score = search_result.documents[0].score if search_result.documents else 0.0

        # 检索覆盖
        retrieval_factor = min(1.0, search_result.total / self.rag_config.top_k)

        # 生成完整性
        gen_factor = min(1.0, gen_result.tokens_generated / 100)

        confidence = (top_score * 0.5 + retrieval_factor * 0.3 + gen_factor * 0.2)
        return min(1.0, max(0.0, confidence))

    # -------------------------------------------------------------------
    # 状态
    # -------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def query_count(self) -> int:
        return self._query_count

    async def get_doc_count(self) -> int:
        """获取知识库文档数"""
        return await self._vector_store.count()

    def get_status(self) -> dict[str, Any]:
        """获取 RAG 引擎状态"""
        return {
            "ready": self._is_ready,
            "query_count": self._query_count,
            "embedding_loaded": self._embedding.is_loaded,
            "vector_store_ready": self._vector_store.is_initialized,
            "llm_loaded": self._llm.is_loaded,
            "knowledge_sync": self._knowledge_sync.get_status(),
        }
