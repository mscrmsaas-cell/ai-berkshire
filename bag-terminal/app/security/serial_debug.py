"""
串口调试接口 — 预留可调试通道

功能:
    - 通过 USB CDC-ACM 串口 (/dev/ttyACM0) 提供调试通道
    - 支持命令行式交互调试
    - 支持日志流式输出
    - 支持系统状态查询

调试命令:
    help              — 显示帮助
    status            — 系统状态
    log <lines>       — 查看最近日志
    config            — 查看配置
    glasses           — 查看眼镜连接
    export list       — 列出导出包
    export full       — 触发全量导出
    ble scan          — BLE 扫描
    ble connect <mac> — 连接眼镜
    reboot            — 重启 (需确认)
    shell <cmd>       — 执行 Shell 命令 (白名单)

安全:
    - 串口调试需输入 Token (与 PC API Token 相同)
    - 速率限制 (每秒最多 10 条命令)
    - 审计日志记录所有命令
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 允许的 Shell 命令白名单
SHELL_WHITELIST = {
    "ls", "df", "free", "uptime", "dmesg", "ip", "ps",
    "cat", "head", "tail", "grep", "wc", "find",
    "sqlite3", "python3",
}


class SerialDebugInterface:
    """
    USB 串口调试接口

    通过 /dev/ttyACM0 提供命令行调试通道。
    PC 端可通过 screen / minicom / putty 连接。
    """

    def __init__(
        self,
        serial_port: str = "/dev/ttyACM0",
        baudrate: int = 115200,
        token_manager: Any = None,
        data_exporter: Any = None,
        db: Any = None,
        settings: Any = None,
    ) -> None:
        self._port = serial_port
        self._baudrate = baudrate
        self._token_manager = token_manager
        self._data_exporter = data_exporter
        self._db = db
        self._settings = settings
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._authenticated: bool = False
        self._last_command_time: float = 0
        self._rate_limit_per_second: int = 10
        self._is_running: bool = False

    async def start(self) -> None:
        """启动串口调试服务"""
        try:
            server = await asyncio.start_server(
                self._handle_client,
                host=None,  # 仅本地 Unix socket 回退
                port=0,
            )
            logger.info("serial_debug.started", port=self._port)
        except Exception as exc:
            logger.warning("serial_debug.start_failed", error=str(exc), port=self._port)
            # 回退: 轮询串口设备
            self._is_running = True
            asyncio.create_task(self._poll_serial(), name="serial-poll")

    async def stop(self) -> None:
        """停止串口调试服务"""
        self._is_running = False
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _poll_serial(self) -> None:
        """轮询串口设备 (等待设备出现)"""
        while self._is_running:
            try:
                import os
                if os.path.exists(self._port):
                    self._writer = await self._open_serial()
                    if self._writer:
                        await self._interactive_loop()
            except Exception as exc:
                logger.debug("serial_debug.poll_error", error=str(exc))
            await asyncio.sleep(2)

    async def _open_serial(self) -> asyncio.StreamWriter | None:
        """打开串口设备"""
        try:
            reader, writer = await asyncio.open_serial(self._port, baudrate=self._baudrate)
            return writer
        except AttributeError:
            # asyncio.open_serial 在某些平台不可用
            # 使用 subprocess + stty 替代
            import subprocess
            subprocess.run(
                ["stty", "-F", self._port, "raw", "echo", "0"],
                check=False, capture_output=True,
            )
            return None
        except Exception as exc:
            logger.debug("serial_debug.open_error", error=str(exc))
            return None

    async def _interactive_loop(self) -> None:
        """交互式命令循环"""
        self._write_line("=" * 50)
        self._write_line("Rail-AR Bag Terminal Debug Console")
        self._write_line(f"Time: {datetime.now(timezone.utc).isoformat()}")
        self._write_line("=" * 50)
        self._write_line("")

        if self._token_manager:
            self._write_line("请输入 Token (查看 OLED 屏): ")
            self._authenticated = False
        else:
            self._authenticated = True

        while self._is_running and self._writer:
            try:
                line = await self._read_line()
                if not line:
                    continue

                # Token 认证
                if not self._authenticated:
                    if self._token_manager and self._token_manager.validate(line.strip()):
                        self._authenticated = True
                        self._write_line("认证成功。输入 'help' 查看可用命令。")
                    else:
                        self._write_line("Token 无效，请重试。")
                    continue

                # 速率限制
                now = time.time()
                if now - self._last_command_time < 0.1:
                    self._write_line("速率过快，请稍候。")
                    continue
                self._last_command_time = now

                # 执行命令
                await self._execute_command(line.strip())

            except Exception as exc:
                logger.error("serial_debug.command_error", error=str(exc))
                self._write_line(f"错误: {exc}")

    async def _execute_command(self, command: str) -> None:
        """执行调试命令"""
        parts = command.split()
        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        # 审计日志
        logger.info("serial_debug.command", command=command, authenticated=self._authenticated)

        if cmd == "help":
            self._write_help()
        elif cmd == "status":
            await self._cmd_status()
        elif cmd == "log":
            await self._cmd_log(args)
        elif cmd == "config":
            await self._cmd_config()
        elif cmd == "glasses":
            await self._cmd_glasses()
        elif cmd == "export":
            await self._cmd_export(args)
        elif cmd == "ble":
            await self._cmd_ble(args)
        elif cmd == "reboot":
            await self._cmd_reboot(args)
        elif cmd == "shell":
            await self._cmd_shell(args)
        elif cmd == "exit" or cmd == "quit":
            self._authenticated = False
            self._write_line("会话结束。")
        else:
            self._write_line(f"未知命令: {cmd}。输入 'help' 查看帮助。")

    def _write_help(self) -> None:
        """显示帮助"""
        help_text = """
可用命令:
  help              显示此帮助
  status            系统状态 (CPU/内存/磁盘/USB/BLE)
  log <lines>       查看最近 N 行日志 (默认 50)
  config            查看当前配置
  glasses           查看已连接眼镜列表
  export list       列出所有导出包
  export full       触发全量数据导出
  export verify <filename>  验证导出包
  ble scan          BLE 扫描眼镜设备
  ble connect <mac> 连接指定眼镜
  reboot            重启系统 (需确认)
  shell <cmd>       执行 Shell 命令 (白名单: ls/df/free/...)
  exit              退出调试会话
"""
        self._write_line(help_text)

    async def _cmd_status(self) -> None:
        """系统状态命令"""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            self._write_line(f"CPU: {cpu}%")
            self._write_line(f"内存: {mem.used // (1024*1024)}MB / {mem.total // (1024*1024)}MB ({mem.percent}%)")
            self._write_line(f"磁盘: {disk.used // (1024*1024*1024)}GB / {disk.total // (1024*1024*1024)}GB ({disk.percent}%)")
            self._write_line(f"时间: {datetime.now(timezone.utc).isoformat()}")
            self._write_line(f"认证: {'是' if self._authenticated else '否'}")
        except Exception as exc:
            self._write_line(f"状态获取失败: {exc}")

    async def _cmd_log(self, args: list[str]) -> None:
        """查看日志命令"""
        lines = 50
        if args:
            try:
                lines = int(args[0])
            except ValueError:
                self._write_line("参数应为数字。")
                return

        try:
            from pathlib import Path
            log_path = Path("/var/log/bag-terminal/app.log")
            if not log_path.exists():
                log_path = Path("/mnt/sdcard/bag-terminal/logs/app.log")
            if log_path.exists():
                content = log_path.read_text(encoding="utf-8", errors="ignore")
                log_lines = content.splitlines()[-lines:]
                for line in log_lines:
                    self._write_line(line)
            else:
                self._write_line("日志文件未找到。")
        except Exception as exc:
            self._write_line(f"读取日志失败: {exc}")

    async def _cmd_config(self) -> None:
        """查看配置命令"""
        if not self._settings:
            self._write_line("配置未加载。")
            return
        config = {
            "device_id": getattr(self._settings, "device_id", "unknown"),
            "ble_max_connections": getattr(self._settings.ble, "max_connections", 4),
            "models": {
                "yolo": getattr(self._settings.models.yolo, "model_path", ""),
                "llm": getattr(self._settings.models.llm, "model_path", ""),
            },
        }
        self._write_line(json.dumps(config, ensure_ascii=False, indent=2))

    async def _cmd_glasses(self) -> None:
        """查看眼镜列表命令"""
        if not self._db:
            self._write_line("数据库未连接。")
            return
        try:
            rows = await self._db.fetch_all(
                "SELECT ble_address, status, battery_level, mode FROM glasses_devices ORDER BY last_seen_at DESC LIMIT 10",
                params=(),
            )
            if not rows:
                self._write_line("无已注册眼镜设备。")
                return
            self._write_line(f"{'BLE地址':20s} {'状态':10s} {'电量':6s} {'模式':10s}")
            self._write_line("-" * 50)
            for row in rows:
                r = dict(row) if hasattr(row, "keys") else row
                self._write_line(
                    f"{r.get('ble_address', ''):20s} "
                    f"{r.get('status', ''):10s} "
                    f"{str(r.get('battery_level', 0)):6s} "
                    f"{r.get('mode', ''):10s}"
                )
        except Exception as exc:
            self._write_line(f"查询失败: {exc}")

    async def _cmd_export(self, args: list[str]) -> None:
        """导出命令"""
        if not args:
            self._write_line("用法: export list | export full | export verify <filename>")
            return

        sub = args[0]
        if sub == "list":
            if self._data_exporter:
                exports = self._data_exporter.list_exports()
                if not exports:
                    self._write_line("无导出包。")
                else:
                    for e in exports:
                        self._write_line(
                            f"{e['filename']:50s} {e['size_mb']:.2f}MB  {e['created_at']}"
                        )
            else:
                self._write_line("导出器未初始化。")

        elif sub == "full":
            if self._data_exporter:
                self._write_line("开始全量导出...")
                result = await self._data_exporter.export_full()
                self._write_line(f"导出完成: {result.export_id}")
                self._write_line(f"文件数: {result.file_count}")
                self._write_line(f"大小: {result.package_size_bytes / (1024*1024):.2f}MB")
                self._write_line(f"签名: {result.sha256_signature[:16]}...")
            else:
                self._write_line("导出器未初始化。")

        elif sub == "verify":
            if len(args) < 2:
                self._write_line("用法: export verify <filename>")
                return
            if self._data_exporter:
                from pathlib import Path
                pkg = str(Path(self._data_exporter._config.export_dir) / args[1])
                result = await self._data_exporter.verify_export(pkg)
                self._write_line(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                self._write_line("导出器未初始化。")
        else:
            self._write_line(f"未知子命令: {sub}")

    async def _cmd_ble(self, args: list[str]) -> None:
        """BLE 命令"""
        if not args:
            self._write_line("用法: ble scan | ble connect <mac>")
            return
        sub = args[0]
        if sub == "scan":
            self._write_line("BLE 扫描中... (10秒)")
            # 这里调用 BleBridge 的扫描功能
            self._write_line("(需要 BleBridge 实例, 此处为预留接口)")
        elif sub == "connect":
            if len(args) < 2:
                self._write_line("用法: ble connect <mac>")
                return
            self._write_line(f"连接 {args[1]}... (需要 BleBridge 实例)")
        else:
            self._write_line(f"未知子命令: {sub}")

    async def _cmd_reboot(self, args: list[str]) -> None:
        """重启命令"""
        if not args or args[0] != "confirm":
            self._write_line("确认重启? 输入 'reboot confirm'")
            return
        self._write_line("系统重启中...")
        import subprocess
        subprocess.run(["reboot"], check=False)

    async def _cmd_shell(self, args: list[str]) -> None:
        """Shell 命令 (白名单)"""
        if not args:
            self._write_line(f"用法: shell <cmd> (白名单: {', '.join(SHELL_WHITELIST)})")
            return

        cmd = args[0]
        if cmd not in SHELL_WHITELIST:
            self._write_line(f"命令 '{cmd}' 不在白名单中。")
            self._write_line(f"允许: {', '.join(SHELL_WHITELIST)}")
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="ignore")
            if output:
                self._write_line(output[:3000])
            err = stderr.decode("utf-8", errors="ignore")
            if err:
                self._write_line(f"STDERR: {err[:1000]}")
        except asyncio.TimeoutError:
            self._write_line("命令超时。")
        except Exception as exc:
            self._write_line(f"执行失败: {exc}")

    def _write_line(self, text: str) -> None:
        """写入一行到串口"""
        if self._writer:
            try:
                self._writer.write((text + "\r\n").encode("utf-8"))
            except Exception:
                pass
        else:
            logger.info("serial_debug.output", line=text)

    async def _read_line(self) -> str:
        """从串口读取一行"""
        if self._reader:
            data = await self._reader.readline()
            return data.decode("utf-8", errors="ignore").strip()
        # 无串口时, 从 stdin 读取 (开发模式)
        return input()
