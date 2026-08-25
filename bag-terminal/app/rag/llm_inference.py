"""
LLM 推理模块 — llama-cpp-python Phi-3-mini Q4

功能:
    - 加载 Phi-3-mini-4k-instruct Q4 GGUF 模型
    - 上下文管理 (对话历史维护)
    - 流式生成 (逐 token 输出)
    - 生成参数配置 (temperature, top_p, max_tokens 等)

模型文件:
    models/phi-3-mini-4k-instruct-q4.gguf

模型规格:
    - 参数量: 3.8B (Q4 量化)
    - 内存占用: ~2GB
    - 上下文窗口: 4096 tokens
    - 推理速度: 2-5 秒 (CM4, 4 线程)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

# llama-cpp-python 延迟导入
try:
    from llama_cpp import Llama, LlamaGrammar
    LLAMA_AVAILABLE = True
except ImportError:
    LLAMA_AVAILABLE = False
    Llama = None  # type: ignore


@dataclass
class ChatMessage:
    """对话消息"""
    role: str           # "system" / "user" / "assistant"
    content: str


@dataclass
class GenerationResult:
    """生成结果"""
    text: str                          # 生成文本
    tokens_generated: int = 0          # 生成 token 数
    elapsed_seconds: float = 0.0       # 耗时 (秒)
    tokens_per_second: float = 0.0     # 生成速度
    model_name: str = ""               # 模型名称


class LlmInference:
    """
    Phi-3-mini Q4 LLM 推理器

    使用 llama-cpp-python 进行 CPU 推理 (ARM NEON 优化)。
    """

    # Phi-3-mini 对话模板
    SYSTEM_PROMPT_TEMPLATE = (
        "你是一个铁路巡检智能助手。你的任务是根据提供的参考资料回答铁路巡检相关问题。\n"
        "请遵守以下规则:\n"
        "1. 只根据参考资料回答, 不要编造信息\n"
        "2. 如果参考资料中没有相关信息, 请说明'根据现有资料, 无法回答此问题'\n"
        "3. 回答简洁准确, 适合语音播报\n"
        "4. 涉及安全规程时, 优先引用具体条款\n\n"
        "参考资料:\n{context}\n"
    )

    # Phi-3 chat format
    PHI3_USER_PREFIX = "<|user|>\n"
    PHI3_ASSISTANT_PREFIX = "<|assistant|>\n"
    PHI3_SYSTEM_PREFIX = "<|system|>\n"
    PHI3_END = "<|end|>\n"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm_config = settings.models.llm

        self._llm: "Llama | None" = None
        self._is_loaded: bool = False
        self._model_path: str = ""
        self._model_name: str = "phi-3-mini-q4"

        # 对话上下文
        self._conversation_history: list[ChatMessage] = []
        self._max_history: int = 5  # 保留最近 5 轮对话

    # -------------------------------------------------------------------
    # 加载
    # -------------------------------------------------------------------

    async def load(self) -> None:
        """异步加载 LLM 模型"""
        model_path = Path(self.llm_config.model_path)
        if not model_path.exists():
            logger.warning(
                "llm.model_not_found",
                path=str(model_path),
            )
            return

        if not LLAMA_AVAILABLE:
            logger.warning("llm.llama_cpp_not_available")
            return

        logger.info(
            "llm.loading",
            path=str(model_path),
            n_ctx=self.llm_config.n_ctx,
            n_threads=self.llm_config.n_threads,
        )

        self._model_path = str(model_path)

        await asyncio.get_event_loop().run_in_executor(
            None,
            self._load_sync,
        )

        self._is_loaded = True
        logger.info("llm.loaded", model=self._model_name)

    def _load_sync(self) -> None:
        """同步加载模型"""
        self._llm = Llama(
            model_path=self._model_path,
            n_ctx=self.llm_config.n_ctx,
            n_gpu_layers=self.llm_config.n_gpu_layers,
            n_threads=self.llm_config.n_threads,
            n_batch=512,
            verbose=False,
            use_mmap=True,
            use_mlock=False,
        )

    async def unload(self) -> None:
        """卸载模型"""
        if self._llm:
            del self._llm
            self._llm = None
        self._is_loaded = False
        self._conversation_history.clear()
        logger.info("llm.unloaded")

    # -------------------------------------------------------------------
    # 生成
    # -------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
    ) -> GenerationResult:
        """
        异步生成文本

        Args:
            prompt: 输入 prompt
            max_tokens: 最大生成 token 数 (None 用配置默认值)
            temperature: 温度
            top_p: top-p 采样
            stop: 停止标记

        Returns:
            GenerationResult: 生成结果
        """
        if not self._is_loaded or not self._llm:
            return GenerationResult(
                text="LLM 模型未加载, 无法生成回答。",
                model_name=self._model_name,
            )

        max_tokens = max_tokens or self.llm_config.max_tokens
        temperature = temperature if temperature is not None else self.llm_config.temperature
        top_p = top_p if top_p is not None else self.llm_config.top_p
        stop = stop or self.llm_config.stop_tokens

        import time as _time
        start = _time.perf_counter()

        # 在线程池中执行 (推理是 CPU 密集型, 会阻塞事件循环)
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            self._generate_sync,
            prompt,
            max_tokens,
            temperature,
            top_p,
            stop,
        )

        elapsed = _time.perf_counter() - start

        text = result["choices"][0]["text"].strip()
        tokens_generated = result["usage"]["completion_tokens"]

        tps = tokens_generated / elapsed if elapsed > 0 else 0.0

        logger.info(
            "llm.generated",
            tokens=tokens_generated,
            elapsed=round(elapsed, 2),
            tps=round(tps, 1),
        )

        return GenerationResult(
            text=text,
            tokens_generated=tokens_generated,
            elapsed_seconds=elapsed,
            tokens_per_second=tps,
            model_name=self._model_name,
        )

    def _generate_sync(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str],
    ) -> dict:
        """同步生成"""
        return self._llm(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            echo=False,
        )

    async def generate_stream(
        self,
        prompt: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """
        流式生成 (逐 token 输出)

        Args:
            prompt: 输入 prompt
            max_tokens: 最大 token 数
            temperature: 温度

        Yields:
            str: 生成的文本片段
        """
        if not self._is_loaded or not self._llm:
            yield "LLM 模型未加载。"
            return

        max_tokens = max_tokens or self.llm_config.max_tokens
        temperature = temperature if temperature is not None else self.llm_config.temperature

        stop = self.llm_config.stop_tokens

        # 在线程池中迭代
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _stream():
            try:
                for chunk in self._llm(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                    stream=True,
                    echo=False,
                ):
                    text = chunk["choices"][0]["text"]
                    if text:
                        asyncio.run_coroutine_threadsafe(
                            queue.put(text),
                            asyncio.get_event_loop(),
                        )
                asyncio.run_coroutine_threadsafe(
                    queue.put(None),
                    asyncio.get_event_loop(),
                )
            except Exception as exc:
                logger.error("llm.stream_error", error=str(exc))
                asyncio.run_coroutine_threadsafe(
                    queue.put(None),
                    asyncio.get_event_loop(),
                )

        # 启动流式生成线程
        import threading
        thread = threading.Thread(target=_stream, daemon=True)
        thread.start()

        # 从队列消费
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    # -------------------------------------------------------------------
    # 对话管理
    # -------------------------------------------------------------------

    def build_rag_prompt(
        self,
        question: str,
        context_docs: list[str],
        system_prompt: str | None = None,
    ) -> str:
        """
        构建 RAG prompt

        Args:
            question: 用户问题
            context_docs: 检索到的文档内容列表
            system_prompt: 自定义系统 prompt (None 用默认)

        Returns:
            完整 prompt 字符串
        """
        # 组装上下文
        context_text = "\n---\n".join(context_docs) if context_docs else "无相关参考资料"

        sys_prompt = system_prompt or self.SYSTEM_PROMPT_TEMPLATE.format(context=context_text)

        # Phi-3-mini chat format
        prompt = (
            f"{self.PHI3_SYSTEM_PREFIX}{sys_prompt}{self.PHI3_END}"
            f"{self.PHI3_USER_PREFIX}{question}{self.PHI3_END}"
            f"{self.PHI3_ASSISTANT_PREFIX}"
        )

        return prompt

    def add_to_history(self, role: str, content: str) -> None:
        """添加消息到对话历史"""
        self._conversation_history.append(ChatMessage(role=role, content=content))
        # 限制历史长度
        if len(self._conversation_history) > self._max_history * 2:
            self._conversation_history = self._conversation_history[-self._max_history * 2:]

    def clear_history(self) -> None:
        """清空对话历史"""
        self._conversation_history.clear()

    # -------------------------------------------------------------------
    # 状态
    # -------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def context_window(self) -> int:
        return self.llm_config.n_ctx

    def get_info(self) -> dict:
        return {
            "loaded": self._is_loaded,
            "model_name": self._model_name,
            "model_path": self._model_path,
            "context_window": self.llm_config.n_ctx,
            "n_threads": self.llm_config.n_threads,
            "max_tokens": self.llm_config.max_tokens,
            "history_length": len(self._conversation_history),
        }
