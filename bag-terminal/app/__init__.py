"""
挎包终端应用包 — 铁路巡检智能眼镜系统

包结构:
    app.main          — FastAPI 入口, 生命周期管理
    app.config        — Pydantic Settings 配置加载
    app.ble           — BLE NUS 网桥 (Bleak Central)
    app.ai            — YOLOv8n ONNX 推理 + 图像处理
    app.rag           — 本地 RAG 引擎 (Qdrant + BGE + Phi-3)
    app.models        — Pydantic 数据模型
    app.communication — MQTT/涂鸦/SaaS API 客户端 (后续批次)
    app.storage       — SQLite + 文件存储 (后续批次)
    app.network       — 5G 模块管理 (后续批次)
"""

__version__ = "1.0.0"
__app_name__ = "railway-bag-terminal"
