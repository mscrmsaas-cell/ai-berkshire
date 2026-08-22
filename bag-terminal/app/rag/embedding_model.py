"""
嵌入模型模块 — BGE-small-zh ONNX

功能:
    - 加载 BGE-small-zh ONNX 模型 (768 维)
    - 文本向量化 (支持批处理)
    - 分词 (使用简易 WordPiece tokenizer, 不依赖 HuggingFace tokenizers)

模型文件:
    models/bge-small-zh.onnx

嵌入维度: 768
最大序列长度: 512
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)


class EmbeddingModel:
    """
    BGE-small-zh ONNX 嵌入模型

    将文本转换为 768 维向量, 用于 RAG 检索。
    """

    # BGE-small-zh 模型常量
    MODEL_DIM = 768
    MAX_SEQ_LENGTH = 512

    def __init__(self, settings: Settings):
        self.settings = settings
        self.embedding_config = settings.models.embedding

        self._session: ort.InferenceSession | None = None
        self._input_ids_name: str = "input_ids"
        self._attention_mask_name: str = "attention_mask"
        self._token_type_ids_name: str = "token_type_ids"
        self._output_name: str = "last_hidden_state"
        self._is_loaded: bool = False
        self._embedding_dim: int = self.embedding_config.embedding_dim

        # 简易 tokenizer 词汇表
        self._vocab: dict[str, int] = {}
        self._vocab_size: int = 0

    async def load(self) -> None:
        """异步加载嵌入模型"""
        model_path = Path(self.embedding_config.model_path)
        if not model_path.exists():
            logger.warning(
                "embedding.model_not_found",
                path=str(model_path),
            )
            return

        logger.info("embedding.loading", path=str(model_path))

        await asyncio.get_event_loop().run_in_executor(
            None,
            self._load_sync,
            str(model_path),
        )

        # 加载词汇表 (如果存在)
        vocab_path = model_path.parent / "vocab.json"
        if vocab_path.exists():
            self._load_vocab(vocab_path)

        self._is_loaded = True
        logger.info(
            "embedding.loaded",
            dim=self._embedding_dim,
            vocab_size=self._vocab_size,
        )

    def _load_sync(self, model_path: str) -> None:
        """同步加载模型"""
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1

        self._session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        # 获取输入/输出名称
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if inputs:
            self._input_ids_name = inputs[0].name
            if len(inputs) > 1:
                self._attention_mask_name = inputs[1].name
            if len(inputs) > 2:
                self._token_type_ids_name = inputs[2].name
        if outputs:
            self._output_name = outputs[0].name

    def _load_vocab(self, vocab_path: Path) -> None:
        """加载词汇表"""
        try:
            data = json.loads(vocab_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._vocab = data
                self._vocab_size = len(data)
            elif isinstance(data, list):
                self._vocab = {token: idx for idx, token in enumerate(data)}
                self._vocab_size = len(data)
        except Exception as exc:
            logger.warning("embedding.vocab_load_error", error=str(exc))

    async def unload(self) -> None:
        """卸载模型"""
        if self._session:
            del self._session
            self._session = None
        self._is_loaded = False
        logger.info("embedding.unloaded")

    # -------------------------------------------------------------------
    # 分词
    # -------------------------------------------------------------------

    def _tokenize(self, text: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        简易中文分词

        策略:
            1. 如果有词汇表, 使用 WordPiece 匹配
            2. 否则, 按字符切分 (中文逐字)
            3. 添加 [CLS] 和 [SEP] 特殊标记
        """
        max_len = min(self.embedding_config.max_seq_length, self.MAX_SEQ_LENGTH)

        # 特殊标记
        cls_token = self._vocab.get("[CLS]", 101)
        sep_token = self._vocab.get("[SEP]", 102)
        pad_token = self._vocab.get("[PAD]", 0)
        unk_token = self._vocab.get("[UNK]", 100)

        # 分词
        tokens: list[int] = [cls_token]

        if self._vocab_size > 0:
            # WordPiece 分词 (粗略实现)
            # 对于中文, 按字符切分后查表
            for char in text:
                token_id = self._vocab.get(char, unk_token)
                tokens.append(token_id)
                if len(tokens) >= max_len - 1:
                    break
        else:
            # 无词汇表: 使用字符的 Unicode 码点 (取模到合理范围)
            for char in text:
                token_id = ord(char) % 30000 + 1000
                tokens.append(token_id)
                if len(tokens) >= max_len - 1:
                    break

        tokens.append(sep_token)

        # 填充到固定长度
        attention_mask = [1] * len(tokens)
        while len(tokens) < max_len:
            tokens.append(pad_token)
            attention_mask.append(0)

        input_ids = np.array([tokens], dtype=np.int64)
        attention_mask_arr = np.array([attention_mask], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        return input_ids, attention_mask_arr, token_type_ids

    # -------------------------------------------------------------------
    # 嵌入
    # -------------------------------------------------------------------

    async def embed(self, text: str) -> np.ndarray:
        """
        将文本嵌入为向量

        Args:
            text: 输入文本

        Returns:
            np.ndarray: (768,) float32 向量
        """
        if not self._is_loaded or not self._session:
            # 返回零向量 (降级处理)
            return np.zeros(self._embedding_dim, dtype=np.float32)

        return await asyncio.get_event_loop().run_in_executor(
            None,
            self._embed_sync,
            text,
        )

    def _embed_sync(self, text: str) -> np.ndarray:
        """同步嵌入"""
        input_ids, attention_mask, token_type_ids = self._tokenize(text)

        # 推理
        outputs = self._session.run(
            [self._output_name],
            {
                self._input_ids_name: input_ids,
                self._attention_mask_name: attention_mask,
                self._token_type_ids_name: token_type_ids,
            },
        )

        # last_hidden_state: (1, seq_len, hidden_dim)
        hidden_state = outputs[0]

        # Mean pooling (考虑 attention mask)
        mask = attention_mask.astype(np.float32)
        mask_expanded = np.expand_dims(mask, axis=-1)  # (1, seq_len, 1)
        sum_embeddings = np.sum(hidden_state * mask_expanded, axis=1)  # (1, hidden_dim)
        sum_mask = np.sum(mask_expanded, axis=1)  # (1, 1)
        sum_mask = np.maximum(sum_mask, 1e-9)

        embedding = (sum_embeddings / sum_mask)[0]  # (hidden_dim,)

        # L2 归一化
        norm = np.linalg.norm(embedding)
        if norm > 1e-9:
            embedding = embedding / norm

        return embedding.astype(np.float32)

    async def embed_batch(self, texts: list[str]) -> np.ndarray:
        """
        批量嵌入

        Args:
            texts: 文本列表

        Returns:
            np.ndarray: (N, 768) float32
        """
        if not texts:
            return np.zeros((0, self._embedding_dim), dtype=np.float32)

        if not self._is_loaded:
            return np.zeros((len(texts), self._embedding_dim), dtype=np.float32)

        # 并发嵌入
        tasks = [self.embed(text) for text in texts]
        results = await asyncio.gather(*tasks)
        return np.stack(results)

    # -------------------------------------------------------------------
    # 状态
    # -------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    @property
    def embedding_dim(self) -> int:
        return self._embedding_dim

    def get_info(self) -> dict[str, Any]:
        return {
            "loaded": self._is_loaded,
            "dim": self._embedding_dim,
            "max_seq_length": self.embedding_config.max_seq_length,
            "vocab_size": self._vocab_size,
        }
