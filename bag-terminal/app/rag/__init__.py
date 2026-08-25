"""
RAG 引擎包 — 本地检索增强生成

子模块:
    embedding_model  — BGE-small-zh ONNX 嵌入模型
    vector_store      — Qdrant 本地模式向量存储 (768 维)
    llm_inference     — llama-cpp-python Phi-3-mini Q4 LLM
    knowledge_sync    — 从 SaaS Dify 增量同步知识库
    local_rag         — RAG 引擎入口 (向量化 → Top-5 检索 → 组装 Prompt → LLM 生成)
"""

from app.rag.embedding_model import EmbeddingModel
from app.rag.vector_store import VectorStore, VectorDocument, SearchResult
from app.rag.llm_inference import LlmInference
from app.rag.knowledge_sync import KnowledgeSync
from app.rag.local_rag import LocalRAG

__all__ = [
    "EmbeddingModel",
    "VectorStore",
    "VectorDocument",
    "SearchResult",
    "LlmInference",
    "KnowledgeSync",
    "LocalRAG",
]
