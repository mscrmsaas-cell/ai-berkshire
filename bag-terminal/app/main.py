"""
挎包终端 FastAPI 主入口

功能:
    - lifespan 生命周期管理 (启动/关闭 BLE 网桥、AI 模型、RAG 引擎)
    - 路由注册 (健康检查、AI 检测、RAG 问答、设备管理)
    - CORS 中间件
    - structlog 结构化日志
    - /health 健康检查端点
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 生命周期管理
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan: 管理应用启动与关闭

    启动阶段:
        1. 加载配置
        2. 初始化 AI 模型管理器 (加载 YOLO/嵌入/LLM)
        3. 初始化 RAG 引擎 (Qdrant + 向量库, 本地离线模式)
        4. 启动 BLE 网桥 (1:1 配对, 扫描并连接预绑定的眼镜)
        5. 初始化数据安全隔离模块 (USB 有线 + .dat 导出)

    关闭阶段:
        1. 停止 BLE 网桥 (断开连接)
        2. 卸载 AI 模型
        3. 关闭 RAG 引擎
        4. 停止安全隔离模块
    """
    settings = get_settings()
    logger.info("bag_terminal.starting", env=settings.env, version="1.0.0")

    # --- 启动阶段 ---
    app.state.settings = settings
    app.state.start_time = datetime.now(timezone.utc)

    # 初始化 AI 模型管理器
    try:
        from app.ai.model_manager import ModelManager
        model_manager = ModelManager(settings)
        await model_manager.initialize()
        app.state.model_manager = model_manager
        logger.info("ai.models_loaded", yolo=settings.models.yolo.model_path)
    except Exception as exc:
        logger.error("ai.models_load_failed", error=str(exc))
        app.state.model_manager = None

    # 初始化 RAG 引擎
    try:
        from app.rag.local_rag import LocalRAG
        rag_engine = LocalRAG(settings)
        await rag_engine.initialize()
        app.state.rag_engine = rag_engine
        logger.info("rag.engine_initialized", collection=settings.rag.collection_name)
    except Exception as exc:
        logger.error("rag.engine_init_failed", error=str(exc))
        app.state.rag_engine = None

    # 启动 BLE 网桥
    try:
        from app.ble.ble_bridge import BleBridge
        ble_bridge = BleBridge(settings)
        app.state.ble_bridge = ble_bridge
        # 后台任务启动扫描循环
        ble_task = asyncio.create_task(ble_bridge.start())
        app.state.ble_task = ble_task
        logger.info("ble.bridge_started", max_connections=settings.ble.max_connections, mode="1:1_pairing")
    except Exception as exc:
        logger.error("ble.bridge_start_failed", error=str(exc))
        app.state.ble_bridge = None
        app.state.ble_task = None

    # --- 初始化数据安全隔离模块 ---
    try:
        from app.security.usb_manager import UsbConnectionManager
        from app.security.data_exporter import DataExporter, ExportConfig
        from app.security.pc_api import create_pc_api_app, TokenManager
        from app.security.serial_debug import SerialDebugInterface
        from app.storage.local_db import LocalDatabase

        usb_manager = UsbConnectionManager()
        app.state.usb_manager = usb_manager

        # 本地数据库
        local_db = LocalDatabase()
        await local_db.connect()
        app.state.local_db = local_db

        # 数据导出器
        export_config = ExportConfig()
        data_exporter = DataExporter(settings, local_db, export_config)
        app.state.data_exporter = data_exporter

        # PC 对接 API (仅绑定 USB 网卡)
        pc_api = create_pc_api_app(usb_manager, data_exporter, local_db, settings)
        app.state.pc_api = pc_api

        # Token 管理器
        token_manager = TokenManager()
        app.state.token_manager = token_manager

        # 串口调试接口
        serial_debug = SerialDebugInterface(
            token_manager=token_manager,
            data_exporter=data_exporter,
            db=local_db,
            settings=settings,
        )
        app.state.serial_debug = serial_debug

        # 启动 USB 连接监控
        await usb_manager.start()

        # USB 连接时自动生成 Token + 启动 PC API
        async def _on_usb_connect(state):
            logger.info("security.usb_connected", mode=state.mode.value, nic=state.nic_name)
            # 生成新 Token (显示在 OLED 上)
            token = token_manager.generate_token()
            logger.info("security.token_generated", token_prefix=token[:4])
            # TODO: 在 OLED 屏显示 Token

            # 启动串口调试
            await serial_debug.start()

        async def _on_usb_disconnect():
            logger.info("security.usb_disconnected")
            await serial_debug.stop()
            # 撤销所有 Token
            if hasattr(token_manager, '_tokens'):
                token_manager._tokens.clear()

        usb_manager.on_connect(_on_usb_connect)
        usb_manager.on_disconnect(_on_usb_disconnect)

        logger.info("security.module_initialized", usb_ip=UsbConnectionManager.USB_NIC_IP)

    except Exception as exc:
        logger.error("security.module_init_failed", error=str(exc))
        app.state.usb_manager = None
        app.state.local_db = None
        app.state.data_exporter = None

    logger.info("bag_terminal.ready", host=settings.server.host, port=settings.server.port)

    yield

    # --- 关闭阶段 ---
    logger.info("bag_terminal.shutting_down")

    # 停止安全隔离模块
    if usb_mgr := getattr(app.state, "usb_manager", None):
        await usb_mgr.stop()
    if serial_dbg := getattr(app.state, "serial_debug", None):
        await serial_dbg.stop()
    if local_db_obj := getattr(app.state, "local_db", None):
        await local_db_obj.close()

    # 停止 BLE 网桥
    if app.state.ble_bridge:
        await app.state.ble_bridge.stop()
    if app.state.ble_task:
        app.state.ble_task.cancel()
        try:
            await app.state.ble_task
        except asyncio.CancelledError:
            pass

    # 关闭 RAG 引擎
    if app_state_rag := getattr(app.state, "rag_engine", None):
        await app_state_rag.shutdown()

    # 卸载 AI 模型
    if model_mgr := getattr(app.state, "model_manager", None):
        await model_mgr.shutdown()

    logger.info("bag_terminal.stopped")


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """创建 FastAPI 应用实例"""
    settings = get_settings()

    app = FastAPI(
        title="铁路巡检智能眼镜 - 挎包终端",
        description="BLE 1:1 配对 + YOLOv8n 边缘推理 + 本地 RAG + .dat 数据导出 (铁路等保安全隔离)",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- 请求日志中间件 ---
    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        start = datetime.now(timezone.utc)
        response = await call_next(request)
        duration_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response

    # --- 全局异常处理 ---
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(
            "http.unhandled_exception",
            path=request.url.path,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "detail": str(exc)},
        )

    # --- 路由注册 ---
    _register_routes(app)

    return app


def _register_routes(app: FastAPI) -> None:
    """注册 API 路由"""

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        """健康检查端点"""
        uptime_seconds = 0.0
        if start_time := getattr(app.state, "start_time", None):
            uptime_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()

        ble_status = "unknown"
        if ble_bridge := getattr(app.state, "ble_bridge", None):
            ble_status = "running" if ble_bridge.is_running else "stopped"

        ai_status = "loaded" if getattr(app.state, "model_manager", None) else "unloaded"
        rag_status = "ready" if getattr(app.state, "rag_engine", None) else "unavailable"

        return {
            "status": "ok",
            "service": "bag-terminal",
            "version": "1.0.0",
            "uptime_seconds": round(uptime_seconds, 1),
            "components": {
                "ble_bridge": ble_status,
                "ai_models": ai_status,
                "rag_engine": rag_status,
            },
            "connected_glasses": len(getattr(app.state, "ble_bridge", None).connected_devices) if app.state.ble_bridge else 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/ble/devices", tags=["ble"])
    async def list_ble_devices() -> dict[str, Any]:
        """列出已连接的 BLE 眼镜设备"""
        ble_bridge = getattr(app.state, "ble_bridge", None)
        if not ble_bridge:
            return {"devices": []}
        devices = []
        for addr, client in ble_bridge.connected_devices.items():
            devices.append({
                "address": addr,
                "name": client.device_name,
                "connected": client.is_connected,
                "battery_level": client.battery_level,
                "rssi": client.rssi,
            })
        return {"devices": devices, "count": len(devices)}

    @app.post("/api/v1/ai/detect", tags=["ai"])
    async def detect_defects(request: Request) -> dict[str, Any]:
        """
        AI 缺陷检测端点

        接收 JPEG 图片数据, 返回检测结果。
        请求体: {"image_base64": "<base64编码的JPEG>", "device_address": "可选, 指定眼镜"}
        """
        model_manager = getattr(app.state, "model_manager", None)
        if not model_manager or not model_manager.is_loaded:
            return JSONResponse(
                status_code=503,
                content={"error": "ai_model_not_loaded", "detail": "YOLO 模型未加载"},
            )

        body = await request.json()
        image_b64 = body.get("image_base64", "")
        if not image_b64:
            return JSONResponse(
                status_code=400,
                content={"error": "missing_image", "detail": "image_base64 字段缺失"},
            )

        import base64

        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_base64", "detail": "base64 解码失败"},
            )

        detections = await model_manager.detect(image_bytes)
        return {
            "detections": detections,
            "count": len(detections),
            "model_version": model_manager.current_version,
        }

    @app.post("/api/v1/rag/query", tags=["rag"])
    async def rag_query(request: Request) -> dict[str, Any]:
        """
        RAG 问答端点

        请求体: {"question": "铁路巡检相关问题"}
        返回: {"answer": "...", "sources": [...], "confidence": 0.x}
        """
        rag_engine = getattr(app.state, "rag_engine", None)
        if not rag_engine:
            return JSONResponse(
                status_code=503,
                content={"error": "rag_unavailable", "detail": "RAG 引擎未初始化"},
            )

        body = await request.json()
        question = body.get("question", "").strip()
        if not question:
            return JSONResponse(
                status_code=400,
                content={"error": "missing_question", "detail": "question 字段缺失"},
            )

        result = await rag_engine.query(question)
        return result

    @app.get("/api/v1/rag/sync", tags=["rag"])
    async def trigger_rag_sync() -> dict[str, Any]:
        """触发知识库增量同步"""
        rag_engine = getattr(app.state, "rag_engine", None)
        if not rag_engine:
            return JSONResponse(
                status_code=503,
                content={"error": "rag_unavailable"},
            )
        synced = await rag_engine.sync_knowledge()
        return {"synced_documents": synced}

    @app.get("/api/v1/models/version", tags=["ai"])
    async def model_version() -> dict[str, Any]:
        """查询当前 AI 模型版本"""
        model_manager = getattr(app.state, "model_manager", None)
        if not model_manager:
            return {"loaded": False}
        return {
            "loaded": model_manager.is_loaded,
            "current_version": model_manager.current_version,
            "available_versions": model_manager.available_versions,
        }

    @app.post("/api/v1/models/hot-update", tags=["ai"])
    async def trigger_hot_update() -> dict[str, Any]:
        """触发模型热更新检查"""
        model_manager = getattr(app.state, "model_manager", None)
        if not model_manager:
            return JSONResponse(status_code=503, content={"error": "model_manager_unavailable"})
        updated = await model_manager.check_and_update()
        return {"updated": updated, "current_version": model_manager.current_version}


# ---------------------------------------------------------------------------
# 应用实例
# ---------------------------------------------------------------------------

app = create_app()
