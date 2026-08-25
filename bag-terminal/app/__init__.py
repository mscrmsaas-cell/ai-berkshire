"""
挎包终端应用包 — 铁路巡检智能眼镜系统 v2.0 (等保安全隔离架构)

包结构:
    app.main          — FastAPI 入口, 生命周期管理
    app.config        — Pydantic Settings 配置加载
    app.ble           — BLE NUS 网桥 (Bleak Central, 1:1 严格配对)
    app.ai            — YOLOv8n ONNX 推理 + 图像处理 (边缘实时)
    app.rag           — 本地 RAG 引擎 (Qdrant + BGE + Phi-3, 离线模式)
    app.models        — Pydantic 数据模型
    app.security      — 数据安全隔离 (USB 有线 + .dat 导出 + Token 认证)
    app.storage       — SQLite + 文件存储
    app.communication — 通信模块 (已禁用外网通信, 仅保留接口)
"""

__version__ = "2.0.0"
__app_name__ = "railway-bag-terminal"
