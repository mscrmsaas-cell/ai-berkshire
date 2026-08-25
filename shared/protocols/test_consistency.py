#!/usr/bin/env python3
"""
test_consistency.py
===================

铁路巡检智能眼镜 — 共享协议一致性测试。

验证以下一致性:
1. C 头文件 ``message_types.h`` 中的枚举值与 Python ``message_types.py`` 一一对应。
2. 涂鸦 DP Schema (``tuya_dp_schema.json``) 完整性校验 (14 个 DP、ID 唯一、字段完整)。
3. 消息类型总数 = 35，范围段 = 8。

运行方式:
    python shared/protocols/test_consistency.py
    # 或从仓库根目录
    make protocol-test

退出码: 0 = 全部通过, 1 = 存在失败

:copyright: Copyright (c) 2024 Railway Inspection AR Glasses Project
:license: Apache-2.0
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径设置 — 使脚本可独立运行
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent  # shared/protocols/
REPO_ROOT = SCRIPT_DIR.parent.parent          # 仓库根目录

# 将 shared/protocols 加入 import 路径，以便导入 message_types
sys.path.insert(0, str(SCRIPT_DIR))

from message_types import (  # noqa: E402
    EXPECTED_COUNT,
    MESSAGE_RANGES,
    MessageType,
)

# 颜色输出
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

PASS = f"{GREEN}[PASS]{RESET}"
FAIL = f"{RED}[FAIL]{RESET}"
INFO = f"{YELLOW}[INFO]{RESET}"

# ---------------------------------------------------------------------------
# 全局测试结果
# ---------------------------------------------------------------------------
_errors: list[str] = []
_tests_run: int = 0


def _record(name: str, success: bool, detail: str = "") -> None:
    """记录单个测试结果。"""
    global _tests_run
    _tests_run += 1
    status = PASS if success else FAIL
    msg = f"  {status} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    if not success:
        _errors.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# 测试 1: 解析 C 头文件枚举值
# ---------------------------------------------------------------------------
def parse_c_header(header_path: Path) -> dict[str, int]:
    """
    解析 message_types.h 中 typedef enum 定义，提取 MSG_xxx = 0xNN 形式的枚举。

    返回 {名称: 整数值} 字典。
    """
    text = header_path.read_text(encoding="utf-8")

    # 匹配:  MSG_NAME = 0xNN,  或  MSG_NAME = 0xNN
    # 只匹配以 MSG_ 或 MSG_TYPE_ 开头的枚举成员
    pattern = re.compile(
        r"^\s*(MSG_[A-Z_]+)\s*=\s*(0x[0-9A-Fa-f]+)\s*,",
        re.MULTILINE,
    )

    result: dict[str, int] = {}
    for match in pattern.finditer(text):
        name = match.group(1)
        value = int(match.group(2), 16)
        result[name] = value

    return result


def test_c_header_valid() -> dict[str, int]:
    """测试 C 头文件可被解析且枚举数量正确。"""
    header_path = SCRIPT_DIR / "message_types.h"
    c_enums = parse_c_header(header_path)

    # 排除 MSG_TYPE_MAX 和 MSG_RANGE_* (这些不是消息类型枚举成员)
    msg_types = {
        k: v for k, v in c_enums.items()
        if not k.startswith("MSG_RANGE_") and k != "MSG_TYPE_MAX"
    }

    _record(
        "C 头文件解析",
        len(msg_types) > 0,
        f"提取到 {len(msg_types)} 个消息类型枚举",
    )
    _record(
        "C 枚举数量 = 35",
        len(msg_types) == EXPECTED_COUNT,
        f"期望 {EXPECTED_COUNT}, 实际 {len(msg_types)}",
    )
    return msg_types


# ---------------------------------------------------------------------------
# 测试 2: Python 枚举完整性
# ---------------------------------------------------------------------------
def test_python_enum() -> dict[str, int]:
    """测试 Python 枚举完整性。"""
    py_enums = MessageType.all_values()

    _record(
        "Python 枚举定义",
        len(py_enums) > 0,
        f"定义了 {len(py_enums)} 个枚举成员",
    )
    _record(
        "Python 枚举数量 = 35",
        len(py_enums) == EXPECTED_COUNT,
        f"期望 {EXPECTED_COUNT}, 实际 {len(py_enums)}",
    )

    # 验证所有值唯一 (@unique 装饰器已保证，但额外检查)
    values = list(py_enums.values())
    _record(
        "Python 枚举值唯一",
        len(values) == len(set(values)),
        f"{len(values)} 个值, {len(set(values))} 个唯一",
    )
    return py_enums


# ---------------------------------------------------------------------------
# 测试 3: C 与 Python 枚举值一一对应
# ---------------------------------------------------------------------------
def test_c_python_consistency(c_enums: dict[str, int], py_enums: dict[str, int]) -> None:
    """比对 C 与 Python 枚举值是否严格一一对应。"""
    # 名称映射: C 使用 MSG_HANDSHAKE_REQ, Python 使用 HANDSHAKE_REQ
    # 需要统一前缀进行比对

    # 构建 Python 名称 → 值 (去掉前缀差异)
    py_normalized = py_enums  # {HANDSHAKE_REQ: 0, ...}

    # C 名称: MSG_HANDSHAKE_REQ → HANDSHAKE_REQ
    c_normalized = {}
    for name, value in c_enums.items():
        short = name
        if short.startswith("MSG_"):
            short = short[4:]  # 去掉 MSG_ 前缀
        c_normalized[short] = value

    # 比对名称集合
    c_names = set(c_normalized.keys())
    py_names = set(py_normalized.keys())

    missing_in_py = c_names - py_names
    missing_in_c = py_names - c_names

    _record(
        "C/Python 名称集合一致",
        not missing_in_py and not missing_in_c,
        f"C 缺少: {missing_in_c or '无'}, Python 缺少: {missing_in_py or '无'}",
    )

    # 比对值
    common_names = c_names & py_names
    value_mismatches = []
    for name in sorted(common_names):
        if c_normalized[name] != py_normalized[name]:
            value_mismatches.append(
                f"{name}: C={c_normalized[name]:#04x} vs Py={py_normalized[name]:#04x}"
            )

    _record(
        "C/Python 枚举值一一对应",
        not value_mismatches,
        f"{len(value_mismatches)} 个不匹配" +
        (f": {'; '.join(value_mismatches[:5])}" if value_mismatches else ""),
    )


# ---------------------------------------------------------------------------
# 测试 4: 消息类型范围段校验
# ---------------------------------------------------------------------------
def test_message_ranges() -> None:
    """验证消息类型分布在 8 个范围段中，且每段至少有 1 个值。"""
    range_counts: dict[int, int] = {base: 0 for base in MESSAGE_RANGES}

    for member in MessageType:
        base = member.range_base
        if base in range_counts:
            range_counts[base] += 1

    all_covered = all(count > 0 for count in range_counts.values())
    detail = ", ".join(
        f"{MESSAGE_RANGES[base]}(0x{base:02X})={count}"
        for base, count in sorted(range_counts.items())
    )
    _record("8 个范围段全部覆盖", all_covered, detail)


# ---------------------------------------------------------------------------
# 测试 5: DP Schema 完整性校验
# ---------------------------------------------------------------------------
def test_dp_schema() -> None:
    """校验涂鸦 DP Schema 的完整性和一致性。"""
    schema_path = SCRIPT_DIR / "tuya_dp_schema.json"

    # 1. JSON 可解析
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _record("DP Schema JSON 解析", True)
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        _record("DP Schema JSON 解析", False, str(exc))
        return

    # 2. 上报 DP 数量 = 10
    upload_dps = schema["data_points"]["upload"]
    _record("上报 DP 数量 = 10", len(upload_dps) == 10,
            f"实际 {len(upload_dps)}")

    # 3. 下行 DP 数量 = 4
    download_dps = schema["data_points"]["download"]
    _record("下行 DP 数量 = 4", len(download_dps) == 4,
            f"实际 {len(download_dps)}")

    # 4. 总数 = 14
    total = len(upload_dps) + len(download_dps)
    _record("DP 总数 = 14", total == 14, f"实际 {total}")

    # 5. ID 唯一性
    all_dps = upload_dps + download_dps
    all_ids = [dp["id"] for dp in all_dps]
    unique_ids = set(all_ids)
    _record("DP ID 唯一性", len(all_ids) == len(unique_ids),
            f"{len(all_ids)} 个 ID, {len(unique_ids)} 个唯一")

    # 6. 上报 ID 范围 1-10
    upload_ids = [dp["id"] for dp in upload_dps]
    upload_range_ok = all(1 <= i <= 10 for i in upload_ids)
    _record("上报 DP ID 范围 1-10", upload_range_ok,
            f"ID: {sorted(upload_ids)}")

    # 7. 下行 ID 范围 101-104
    download_ids = [dp["id"] for dp in download_dps]
    download_range_ok = all(101 <= i <= 104 for i in download_ids)
    _record("下行 DP ID 范围 101-104", download_range_ok,
            f"ID: {sorted(download_ids)}")

    # 8. 必填字段检查
    required_fields = ["id", "code", "name_cn", "name_en", "type", "mode", "direction"]
    missing_fields = []
    for dp in all_dps:
        for field in required_fields:
            if field not in dp:
                missing_fields.append(f"DP {dp.get('id', '?')}: 缺少 {field}")
    _record("DP 必填字段完整", not missing_fields,
            "; ".join(missing_fields[:5]) if missing_fields else "全部完整")

    # 9. enum 类型 DP 必须有 property.range
    enum_without_range = []
    for dp in all_dps:
        if dp["type"] == "enum":
            prop = dp.get("property", {})
            if "range" not in prop or len(prop["range"]) == 0:
                enum_without_range.append(dp["code"])
    _record("enum 类型 DP 有 range 定义", not enum_without_range,
            f"缺少 range: {enum_without_range or '无'}")

    # 10. direction 字段一致性
    upload_dir_ok = all(dp["direction"] == "upload" for dp in upload_dps)
    download_dir_ok = all(dp["direction"] == "download" for dp in download_dps)
    _record("DP direction 字段一致", upload_dir_ok and download_dir_ok,
            f"上传方向正确={upload_dir_ok}, 下行方向正确={download_dir_ok}")


# ---------------------------------------------------------------------------
# 测试 6: ACK 消息值特殊校验 (0x0F)
# ---------------------------------------------------------------------------
def test_special_values() -> None:
    """验证特殊消息类型值符合协议规范。"""
    checks = [
        ("HANDSHAKE_REQ = 0x00", MessageType.HANDSHAKE_REQ, 0x00),
        ("HANDSHAKE_ACK = 0x01", MessageType.HANDSHAKE_ACK, 0x01),
        ("HEARTBEAT = 0x02", MessageType.HEARTBEAT, 0x02),
        ("DISCONNECT = 0x03", MessageType.DISCONNECT, 0x03),
        ("ACK = 0x0F", MessageType.ACK, 0x0F),
        ("OTA_NOTIFY = 0x70", MessageType.OTA_NOTIFY, 0x70),
        ("NAVIGATION = 0x83", MessageType.NAVIGATION, 0x83),
    ]
    for name, member, expected in checks:
        _record(name, member == expected,
                f"值为 {member.value:#04x}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> int:
    """运行所有一致性测试，返回退出码。"""
    print(f"\n{INFO} 铁路巡检智能眼镜 — 协议一致性测试")
    print(f"{INFO} 仓库根目录: {REPO_ROOT}")
    print(f"{INFO} 协议目录: {SCRIPT_DIR}\n")

    print("=" * 60)
    print("测试 1-2: C 头文件与 Python 枚举解析")
    print("=" * 60)
    c_enums = test_c_header_valid()
    py_enums = test_python_enum()

    print()
    print("=" * 60)
    print("测试 3: C/Python 枚举值一致性比对")
    print("=" * 60)
    test_c_python_consistency(c_enums, py_enums)

    print()
    print("=" * 60)
    print("测试 4: 消息类型范围段覆盖")
    print("=" * 60)
    test_message_ranges()

    print()
    print("=" * 60)
    print("测试 5: 涂鸦 DP Schema 完整性")
    print("=" * 60)
    test_dp_schema()

    print()
    print("=" * 60)
    print("测试 6: 特殊消息类型值校验")
    print("=" * 60)
    test_special_values()

    # 汇总
    print()
    print("=" * 60)
    passed = _tests_run - len(_errors)
    if _errors:
        print(f"{FAIL} {len(_errors)} 个测试失败 (共 {_tests_run} 个)")
        for err in _errors:
            print(f"  - {err}")
        print("=" * 60)
        return 1
    else:
        print(f"{PASS} 全部 {passed} 个测试通过")
        print("=" * 60)
        return 0


if __name__ == "__main__":
    sys.exit(main())
