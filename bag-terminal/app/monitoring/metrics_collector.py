"""Prometheus 指标采集器。

负责采集和暴露挎包终端各子系统的 Prometheus 指标：
- BLE 连接数、吞吐量
- AI 推理耗时、检测数
- RAG 查询数、响应时间
- 5G 信号强度、网络状态
- 充电状态、电池电量
- 照片上传数、同步队列深度
- OTA 升级进度
- 系统资源（CPU/内存/磁盘/温度）

指标通过 /metrics 端点以 Prometheus exposition format 暴露。
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

import structlog
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

logger = structlog.get_logger(__name__)


@dataclass
class GaugeMetric:
    """Gauge 指标（可增可减的当前值）。"""

    name: str
    help_text: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> None:
        self.value = value


@dataclass
class CounterMetric:
    """Counter 指标（只增不减的累计值）。"""

    name: str
    help_text: str
    value: float = 0.0
    label_values: dict[str, float] = field(default_factory=dict)

    def inc(self, amount: float = 1.0, label: str = "") -> None:
        if label:
            self.label_values[label] = self.label_values.get(label, 0) + amount
        else:
            self.value += amount


@dataclass
class HistogramMetric:
    """Histogram 指标（延迟分布）。"""

    name: str
    help_text: str
    buckets: list[float] = field(default_factory=lambda: [
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0
    ])
    counts: list[int] = field(default_factory=lambda: [0] * 11)
    sum_value: float = 0.0
    total_count: int = 0
    recent_values: deque = field(default_factory=lambda: deque(maxlen=1000))

    def observe(self, value: float) -> None:
        self.sum_value += value
        self.total_count += 1
        self.recent_values.append(value)
        for i, bucket in enumerate(self.buckets):
            if value <= bucket:
                self.counts[i] += 1

    @property
    def avg(self) -> float:
        return self.sum_value / self.total_count if self.total_count > 0 else 0.0

    @property
    def p95(self) -> float:
        if not self.recent_values:
            return 0.0
        sorted_vals = sorted(self.recent_values)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]


class MetricsCollector:
    """Prometheus 指标采集器。

    使用示例::

        collector = MetricsCollector()
        # 注册到 FastAPI
        register_metrics_endpoint(app, collector)
        # 更新指标
        collector.set_ble_connections(2)
        collector.observe_ai_inference(0.045)
    """

    def __init__(self) -> None:
        # ── BLE 指标 ──
        self.ble_connections = GaugeMetric(
            "bag_ble_connections",
            "Current BLE connected glasses count",
        )
        self.ble_bytes_sent = CounterMetric(
            "bag_ble_bytes_sent_total",
            "Total bytes sent over BLE",
        )
        self.ble_bytes_received = CounterMetric(
            "bag_ble_bytes_received_total",
            "Total bytes received over BLE",
        )
        self.ble_reconnect_count = CounterMetric(
            "bag_ble_reconnect_total",
            "BLE reconnection count",
        )

        # ── AI 指标 ──
        self.ai_inference_duration = HistogramMetric(
            "bag_ai_inference_duration_seconds",
            "YOLO inference latency",
        )
        self.ai_detections_total = CounterMetric(
            "bag_ai_detections_total",
            "Total AI detections by class",
        )
        self.ai_model_loaded = GaugeMetric(
            "bag_ai_model_loaded",
            "AI model loaded status (1=loaded, 0=unloaded)",
        )

        # ── RAG 指标 ──
        self.rag_query_duration = HistogramMetric(
            "bag_rag_query_duration_seconds",
            "RAG query response time",
        )
        self.rag_queries_total = CounterMetric(
            "bag_rag_queries_total",
            "Total RAG queries",
        )
        self.rag_fallback_total = CounterMetric(
            "bag_rag_fallback_total",
            "RAG remote fallback count",
        )

        # ── 网络指标 ──
        self.network_signal_rsrp = GaugeMetric(
            "bag_network_signal_rsrp_dbm",
            "5G/LTE signal RSRP in dBm",
        )
        self.network_online = GaugeMetric(
            "bag_network_online",
            "Network online status (1=online, 0=offline)",
        )
        self.network_type = GaugeMetric(
            "bag_network_type",
            "Active network type (0=none, 1=5G, 2=WiFi)",
        )

        # ── 充电指标 ──
        self.battery_level = GaugeMetric(
            "bag_battery_level_percent",
            "Battery level percentage",
        )
        self.battery_voltage = GaugeMetric(
            "bag_battery_voltage_mv",
            "Battery voltage in mV",
        )
        self.battery_temperature = GaugeMetric(
            "bag_battery_temperature_celsius",
            "Battery temperature",
        )
        self.charging_status = GaugeMetric(
            "bag_charging_status",
            "Charging status (1=charging, 0=not charging)",
        )

        # ── 同步指标 ──
        self.sync_queue_depth = GaugeMetric(
            "bag_sync_queue_depth",
            "Pending sync queue depth",
        )
        self.sync_success_total = CounterMetric(
            "bag_sync_success_total",
            "Total successful syncs",
        )
        self.sync_failed_total = CounterMetric(
            "bag_sync_failed_total",
            "Total failed syncs",
        )
        self.sync_duration = HistogramMetric(
            "bag_sync_duration_seconds",
            "Sync operation duration",
        )

        # ── 照片上传指标 ──
        self.photos_uploaded = CounterMetric(
            "bag_photos_uploaded_total",
            "Total photos uploaded to SaaS",
        )
        self.photos_pending = GaugeMetric(
            "bag_photos_pending",
            "Pending photo uploads",
        )

        # ── OTA 指标 ──
        self.ota_progress = GaugeMetric(
            "bag_ota_progress_percent",
            "OTA upgrade progress percentage",
        )
        self.ota_total = CounterMetric(
            "bag_ota_total",
            "Total OTA upgrades",
        )
        self.ota_success = CounterMetric(
            "bag_ota_success_total",
            "Successful OTA upgrades",
        )

        # ── 系统资源指标 ──
        self.cpu_usage = GaugeMetric(
            "bag_cpu_usage_percent",
            "CPU usage percentage",
        )
        self.memory_usage = GaugeMetric(
            "bag_memory_usage_percent",
            "Memory usage percentage",
        )
        self.disk_usage = GaugeMetric(
            "bag_disk_usage_percent",
            "Disk usage percentage",
        )
        self.cpu_temperature = GaugeMetric(
            "bag_cpu_temperature_celsius",
            "CPU temperature",
        )
        self.uptime = GaugeMetric(
            "bag_uptime_seconds",
            "Process uptime in seconds",
        )

        # 告警指标
        self.alerts_total = CounterMetric(
            "bag_alerts_total",
            "Total alerts by type and severity",
        )

        # 收集所有指标
        self._gauges = [
            self.ble_connections, self.ai_model_loaded,
            self.network_signal_rsrp, self.network_online, self.network_type,
            self.battery_level, self.battery_voltage, self.battery_temperature,
            self.charging_status, self.sync_queue_depth,
            self.photos_pending, self.ota_progress,
            self.cpu_usage, self.memory_usage, self.disk_usage,
            self.cpu_temperature, self.uptime,
        ]
        self._counters = [
            self.ble_bytes_sent, self.ble_bytes_received, self.ble_reconnect_count,
            self.ai_detections_total, self.rag_queries_total, self.rag_fallback_total,
            self.sync_success_total, self.sync_failed_total,
            self.photos_uploaded, self.ota_total, self.ota_success,
            self.alerts_total,
        ]
        self._histograms = [
            self.ai_inference_duration, self.rag_query_duration, self.sync_duration,
        ]

        self._start_time = time.time()
        self._update_task: asyncio.Task | None = None

    # ── 便捷更新方法 ──────────────────────────────────────────────

    def set_ble_connections(self, count: int) -> None:
        self.ble_connections.set(count)

    def record_ble_traffic(self, sent: int = 0, received: int = 0) -> None:
        if sent:
            self.ble_bytes_sent.inc(sent)
        if received:
            self.ble_bytes_received.inc(received)

    def observe_ai_inference(self, duration_seconds: float) -> None:
        self.ai_inference_duration.observe(duration_seconds)

    def record_ai_detection(self, class_name: str) -> None:
        self.ai_detections_total.inc(label=class_name)

    def observe_rag_query(self, duration_seconds: float, fallback: bool = False) -> None:
        self.rag_query_duration.observe(duration_seconds)
        self.rag_queries_total.inc()
        if fallback:
            self.rag_fallback_total.inc()

    def set_network_status(self, online: bool, rsrp: int = 0, net_type: int = 0) -> None:
        self.network_online.set(1 if online else 0)
        self.network_signal_rsrp.set(rsrp)
        self.network_type.set(net_type)

    def set_battery_status(
        self, level: int, voltage_mv: int = 0, temp_c: float = 0.0, charging: bool = False
    ) -> None:
        self.battery_level.set(level)
        self.battery_voltage.set(voltage_mv)
        self.battery_temperature.set(temp_c)
        self.charging_status.set(1 if charging else 0)

    def set_sync_queue_depth(self, depth: int) -> None:
        self.sync_queue_depth.set(depth)

    def record_sync(self, success: bool, duration: float = 0) -> None:
        if success:
            self.sync_success_total.inc()
        else:
            self.sync_failed_total.inc()
        if duration > 0:
            self.sync_duration.observe(duration)

    def record_photo_upload(self) -> None:
        self.photos_uploaded.inc()

    def set_ota_progress(self, percent: float) -> None:
        self.ota_progress.set(percent)

    def record_ota(self, success: bool) -> None:
        self.ota_total.inc()
        if success:
            self.ota_success.inc()

    def record_alert(self, alert_type: str, severity: str) -> None:
        label = f"{alert_type}_{severity}"
        self.alerts_total.inc(label=label)

    # ── 系统指标自动采集 ──────────────────────────────────────────

    async def start_auto_collect(self, interval: float = 15.0) -> None:
        """启动后台自动采集系统指标。"""
        self._update_task = asyncio.create_task(
            self._collect_loop(interval), name="metrics-collect"
        )

    async def stop_auto_collect(self) -> None:
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

    async def _collect_loop(self, interval: float) -> None:
        while True:
            try:
                self._collect_system_metrics()
                self.uptime.set(time.time() - self._start_time)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("metrics_collect_error", error=str(exc))
            await asyncio.sleep(interval)

    def _collect_system_metrics(self) -> None:
        """采集 CPU/内存/磁盘/温度。"""
        # 内存
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            meminfo = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024
            total = meminfo.get("MemTotal", 1)
            available = meminfo.get("MemAvailable", 0)
            usage = (1 - available / total) * 100
            self.memory_usage.set(round(usage, 1))
        except Exception:  # noqa: BLE001
            pass

        # CPU 使用率（简化：通过 /proc/stat 两次采样）
        try:
            with open("/proc/loadavg", "r") as f:
                loadavg = f.read().split()
                self.cpu_usage.set(round(float(loadavg[0]) * 100, 1))
        except Exception:  # noqa: BLE001
            pass

        # 磁盘
        try:
            stat = os.statvfs("/")
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            usage = (1 - free / total) * 100 if total > 0 else 0
            self.disk_usage.set(round(usage, 1))
        except Exception:  # noqa: BLE001
            pass

        # CPU 温度
        try:
            for path in ["/sys/class/thermal/thermal_zone0/temp"]:
                if os.path.exists(path):
                    with open(path, "r") as f:
                        temp = int(f.read().strip()) / 1000.0
                        self.cpu_temperature.set(temp)
                        break
        except Exception:  # noqa: BLE001
            pass

    # ── Prometheus 格式输出 ───────────────────────────────────────

    def render_prometheus(self) -> str:
        """渲染 Prometheus exposition format 文本。"""
        lines: list[str] = []

        # Gauges
        for gauge in self._gauges:
            lines.append(f"# HELP {gauge.name} {gauge.help_text}")
            lines.append(f"# TYPE {gauge.name} gauge")
            lines.append(f'{gauge.name} {gauge.value}')

        # Counters
        for counter in self._counters:
            lines.append(f"# HELP {counter.name} {counter.help_text}")
            lines.append(f"# TYPE {counter.name} counter")
            if counter.label_values:
                for label, val in counter.label_values.items():
                    escaped = label.replace("\\", "\\\\").replace('"', '\\"')
                    lines.append(f'{counter.name}{{label="{escaped}"}} {val}')
            lines.append(f'{counter.name} {counter.value}')

        # Histograms
        for hist in self._histograms:
            lines.append(f"# HELP {hist.name} {hist.help_text}")
            lines.append(f"# TYPE {hist.name} histogram")
            cumulative = 0
            for i, bucket in enumerate(hist.buckets):
                cumulative += hist.counts[i]
                lines.append(
                    f'{hist.name}_bucket{{le="{bucket}"}} {cumulative}'
                )
            lines.append(f'{hist.name}_bucket{{le="+Inf"}} {hist.total_count}')
            lines.append(f'{hist.name}_sum {hist.sum_value}')
            lines.append(f'{hist.name}_count {hist.total_count}')

        return "\n".join(lines) + "\n"


# ── FastAPI 端点注册 ────────────────────────────────────────────

def register_metrics_endpoint(app: FastAPI, collector: MetricsCollector) -> None:
    """将 /metrics 端点注册到 FastAPI 应用。

    :param app: FastAPI 实例
    :param collector: MetricsCollector 实例
    """

    @app.get("/metrics", tags=["监控"])
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=collector.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
